"""GUI automation adapter: drive Electron agents through their real UI.

The adapter is capability-based: it only works for agents explicitly registered
as ``gui_automation`` with a known app profile. It sends the control-plane
prompt through the app's window, waits on the agent's own transcript for
completion, then returns a strict structured result package.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from ..control.interfaces import (
    prepare_qoder_session, prepare_workbuddy_task, qoder_model_key,
    set_model_programmatically, verify_qoder_session, verify_workbuddy_task,
    workbuddy_model_key,
)
from ..gui import (
    GUI_ERROR_CODES, GuiAutomationError, GuiProfile, GuiReply, WindowsGuiDriver,
    create_reader, find_window, profile_for_app, resolve_model_key,
)
from ..models import AgentRecord, ResultPackage
from ..util import (
    atomic_write_text, ensure_dir, estimate_tokens, now_iso, read_json, read_text,
    tail_text, write_json,
)
from .base import BaseAdapter, ExecutionOutcome, ExecutionRequest, ExecutionUsage

MARKER_PREFIX = "AGENTMESH_GUI_MARKER"


class GuiAutomationAdapter(BaseAdapter):
    def __init__(self, agent: AgentRecord, config: dict[str, Any] | None = None, *,
                 driver: Any = None, reader: Any = None):
        super().__init__(agent, config)
        self._driver = driver
        self._reader = reader
        self.no_gui = (str(self.config.get("no_gui_fallback") or "").lower() in ("1", "true", "yes")
                       or os.environ.get("AGENTMESH_DISABLE_GUI_INPUT") == "1")
        if self.no_gui and str(self.config.get("gui_app") or "") == "qoder_cn":
            os.environ.setdefault("AGENTMESH_QODER_CDP_PORT", "9222")

    # -- public contract -------------------------------------------------
    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        started = time.monotonic()
        gui_dir = ensure_dir(Path(request.task_dir) / "gui")
        log_path = gui_dir / "gui.log"
        before_shot = gui_dir / "before.png"
        after_shot = gui_dir / "after.png"
        started_epoch = time.time() - 1
        marker = f"{MARKER_PREFIX}_{uuid.uuid4().hex}"
        screenshots: list[str] = []
        reply: GuiReply | None = None
        driver = self._driver
        interface_info: dict[str, Any] = {"method": "gui"}
        try:
            profile = self._profile()
            prompt_text = read_text(request.prompt_path)
            if not prompt_text.strip():
                raise GuiAutomationError("GUI_SEND_FAILED", "prompt.md 为空")
            prompt_with_marker = (
                prompt_text
                + "\n\n---\n"
                + f"<!-- {MARKER_PREFIX}: {marker} -->\n"
            )
            driver = driver or WindowsGuiDriver(profile, self.config, log_path=log_path)
            reader = self._reader or create_reader(profile, self.config)
            interface_info = self._prepare_interface(
                profile, request, prompt_with_marker, marker, log_path, started_epoch,
            )
            window = self._attach(driver, profile)
            _log(log_path, "attached", window=window, method=interface_info.get("method"))
            model_key = interface_info.get("model_key") or \
                resolve_model_key(request.model, profile.model_aliases)

            if interface_info.get("method") == "qoder_db_transcript":
                if self.no_gui:
                    # prepare_qoder_session already wrote the model into
                    # main.sqlite and the transcript runtime-config; the
                    # desktop restores that session on restart, so neither the
                    # workspace nor the model needs a mouse click.  The
                    # desktop shows a boot splash for ~15s before the composer
                    # exists, so wait for it before keyboard-only send.
                    time.sleep(float(self.config.get("qoder_ready_seconds") or 18))
                    _log(log_path, "model_programmatic", model=model_key,
                         path="db+transcript", no_gui=True)
                else:
                    # The desktop restores its last route after the restart;
                    # activate the workspace row and open a new task inside it.
                    time.sleep(2.0)
                    try:
                        if driver.select_workspace(Path(request.workdir).name):
                            _log(log_path, "workspace_selected", workspace=request.workdir)
                    except Exception as exc:
                        _log(log_path, "workspace_select_failed", error=str(exc))
            if interface_info.get("method") == "qoder_db_transcript" and model_key and not self.no_gui:
                # Diagnostic-only GUI fallback; forbidden in zero-mouse mode.
                try:
                    if driver.select_model(model_key):
                        interface_info["model_method"] = "gui_fallback"
                        _log(log_path, "model_gui_fallback", model=model_key,
                             reason="diagnostic fallback")
                except Exception as exc:
                    _log(log_path, "model_gui_fallback_failed", model=model_key,
                         error=str(exc))

            if interface_info.get("method") == "gui":
                if self.no_gui:
                    raise GuiAutomationError(
                        "MODEL_GUI_FORBIDDEN",
                        "零鼠标模式没有可用的程序化模型接口，禁止回退到 GUI 点击",
                    )
                if hasattr(driver, "prepare_new_task"):
                    try:
                        if driver.prepare_new_task():
                            _log(log_path, "new_task_ready")
                    except Exception as exc:
                        _log(log_path, "new_task_failed", error=str(exc))
            if interface_info.get("method") == "gui" and model_key:
                if profile.model_switch_supported:
                    if not driver.select_model(model_key):
                        raise GuiAutomationError(
                            "GUI_MODEL_SWITCH_FAILED",
                            f"{profile.product} 无法切换到模型 {model_key}",
                        )
                    _log(log_path, "model_selected", model=model_key)
                elif model_key not in set(profile.model_aliases.values()):
                    raise GuiAutomationError(
                        "GUI_MODEL_SWITCH_FAILED",
                        f"{profile.product} 的 UI 模型切换尚未验证，无法从默认模型切换到 {model_key}",
                        details={"requested": request.model, "canonical": model_key,
                                 "supported": sorted(set(profile.model_aliases.values()))},
                    )
                else:
                    _log(log_path, "model_default", model=model_key,
                         note="依赖应用默认/当前模型，回复结束后按 transcript 校验")

            shot = driver.screenshot(before_shot)
            if shot:
                screenshots.append(shot)
            prompt_to_send = interface_info.get("short_prompt") or prompt_with_marker
            if interface_info.get("method") == "workbuddy_deeplink" and self.no_gui \
                    and hasattr(driver, "submit_keyboard"):
                # The deep link only pre-fills the home composer in some
                # states; Enter submits it without any mouse event.
                driver.submit_keyboard()
                _log(log_path, "prompt_submitted", marker=marker,
                     chars=len(prompt_to_send), method="deeplink+keyboard")
            elif interface_info.get("method") == "workbuddy_deeplink":
                driver.send_prompt(prompt_to_send)
                _log(log_path, "prompt_sent", marker=marker, chars=len(prompt_to_send),
                     method=interface_info.get("method"))
            elif self.no_gui and interface_info.get("method") == "qoder_db_transcript":
                from ..gui.cdp import send_prompt_via_cdp
                cdp_result = send_prompt_via_cdp(prompt_to_send)
                _log(log_path, "prompt_sent", marker=marker, chars=len(prompt_to_send),
                     method=interface_info.get("method"), input="cdp", cdp=cdp_result)
            elif self.no_gui and hasattr(driver, "send_prompt_keyboard"):
                driver.send_prompt_keyboard(prompt_to_send)
                _log(log_path, "prompt_sent", marker=marker, chars=len(prompt_to_send),
                     method=interface_info.get("method"), input="keyboard")
            else:
                driver.send_prompt(prompt_to_send)
                _log(log_path, "prompt_sent", marker=marker, chars=len(prompt_to_send),
                     method=interface_info.get("method"))
            timeout = float(request.timeout_seconds or profile.timeout_seconds)
            # Never stall earlier than the measured profile threshold; the
            # discovery-written adapter_config may still carry an old 90s.
            stall_seconds = max(float(self.config.get("stall_seconds") or 0),
                                float(profile.stall_seconds))
            reply = reader.wait_reply(
                marker, started_at=started_epoch, timeout=timeout,
                stall_seconds=stall_seconds, expected_model=model_key,
            )
            _log(log_path, "reply_received", session_id=reply.session_id,
                 model=reply.model, events=reply.events, chars=len(reply.text))
            verify = interface_info.get("verify")
            if verify:
                if callable(verify):
                    outcome = verify(reply.session_id)
                else:
                    outcome = verify
                interface_info["verify"] = outcome.to_dict()
                _log(log_path, "interface_verify", **outcome.to_dict())
                if not outcome.ok:
                    raise GuiAutomationError(
                        outcome.error_code or "WORKSPACE_MISMATCH",
                        outcome.reason or "程序接口回读校验失败",
                        details=outcome.to_dict(),
                    )
            shot = driver.screenshot(after_shot)
            if shot:
                screenshots.append(shot)
            return self._finish_success(request, reply, profile, model_key, screenshots,
                                        log_path, started, interface_info)
        except GuiAutomationError as exc:
            if driver:
                try:
                    shot = driver.screenshot(gui_dir / "error.png")
                    if shot:
                        screenshots.append(shot)
                except Exception:
                    pass
            _log(log_path, "error", code=exc.code, message=exc.message,
                 details=exc.details, screenshots=screenshots)
            status = "timeout" if exc.code in ("GUI_TIMEOUT", "GUI_STALLED") else "failed"
            return ExecutionOutcome(
                status=status, error=f"{exc.code}: {exc.message}", error_code=exc.code,
                logs=tail_text(log_path, max_chars=20000),
                duration_ms=_duration_ms(started), session_id=(reply.session_id if reply else ""),
                extras={"gui_app": profile.app if 'profile' in locals() else "",
                        "screenshots": screenshots, "error_code": exc.code,
                        "details": exc.details,
                        "interface": {k: ("<callable>" if callable(v) else v)
                                      for k, v in interface_info.items()}},
                text=(reply.text if reply else ""),
                result_payload=None, result_path=request.result_path,
            )
        except Exception as exc:  # unexpected driver/parse failure must stay diagnosable
            _log(log_path, "error", code="GUI_DRIVER_ERROR", message=str(exc))
            return ExecutionOutcome(
                status="failed", error=f"GUI_DRIVER_ERROR: {exc}", error_code="GUI_DRIVER_ERROR",
                logs=tail_text(log_path, max_chars=20000),
                duration_ms=_duration_ms(started),
                extras={"screenshots": screenshots},
            )
        finally:
            if driver:
                try:
                    driver.close()
                except Exception:
                    pass

    # -- programmatic interface -------------------------------------------
    def _prepare_interface(self, profile: GuiProfile, request: ExecutionRequest,
                           prompt_with_marker: str, marker: str,
                           log_path: Path, started_epoch: float) -> dict[str, Any]:
        """Use the agent's own launch/store interface before touching the GUI."""
        app = str(profile.app or "")
        workspace = str(request.workdir or "")
        if self._driver is not None or self._reader is not None:
            return {"method": "gui", "fallback_reason": "injected stub driver/reader"}
        if not workspace:
            return {"method": "gui", "fallback_reason": "no workspace"}
        if app == "workbuddy":
            model_key = workbuddy_model_key(request.model)
            welcome = str(self.config.get("workbuddy_welcome_mode") or "code")
            permission = str(self.config.get("workbuddy_permission_mode") or "fullAccess")
            prompt_dir = ensure_dir(Path(workspace) / ".agentmesh")
            prompt_copy = prompt_dir / "interface-prompt.md"
            atomic_write_text(prompt_copy, prompt_with_marker)
            short_prompt = (
                f"请先读取并严格执行 `{prompt_copy}` 中的任务说明，"
                "完成后按其中的输出契约写结果。\n"
                f"<!-- {MARKER_PREFIX}: {marker} -->\n"
            )
            outcome = prepare_workbuddy_task(
                workspace=workspace, model=request.model, prompt=short_prompt,
                welcome_mode=welcome, permission_mode=permission, config=self.config,
            )
            _log(log_path, "interface_prepare", app=app, **outcome.to_dict())
            if not outcome.ok:
                raise GuiAutomationError(
                    outcome.error_code or "WORKSPACE_SWITCH_FAILED",
                    outcome.reason or "WorkBuddy 深链接准备失败",
                    details={**outcome.to_dict(), "prompt_copy": str(prompt_copy)},
                )
            return {
                "method": "workbuddy_deeplink", "model_key": model_key,
                "short_prompt": short_prompt, "prepare": outcome.to_dict(),
                "prompt_file": str(prompt_copy),
                "verify": lambda _sid="": verify_workbuddy_task(
                    workspace=workspace, model=request.model,
                    started_at=started_epoch, welcome_mode=welcome,
                    permission_mode=permission, config=self.config,
                ),
            }
        if app == "qoder_cn":
            model_key = qoder_model_key(request.model) or "dfmodel"
            prompt_dir = ensure_dir(Path(workspace) / ".agentmesh")
            prompt_copy = prompt_dir / "interface-prompt.md"
            atomic_write_text(prompt_copy, prompt_with_marker)
            short_prompt = (
                f"请先读取并严格执行 `{prompt_copy}` 中的任务说明，"
                "完成后按其中的输出契约写结果。\n"
                f"<!-- {MARKER_PREFIX}: {marker} -->\n"
            )
            outcome = prepare_qoder_session(
                workspace=workspace, model=model_key, config=self.config,
            )
            _log(log_path, "interface_prepare", app=app, **outcome.to_dict())
            if not outcome.ok:
                fallback = set_model_programmatically(
                    "qoder_cn", request.model, self.config,
                ) if request.model else None
                _log(log_path, "interface_fallback", reason=outcome.reason,
                     fallback=(fallback.to_dict() if fallback else {}),
                     note="工作区切换没有 GUI 兜底，直接中止")
                raise GuiAutomationError(
                    outcome.error_code or "WORKSPACE_SWITCH_FAILED",
                    outcome.reason or "Qoder 会话工作区写入失败",
                    details={**outcome.to_dict(),
                             "fallback": fallback.to_dict() if fallback else {}},
                )
            return {
                "method": "qoder_db_transcript", "model_key": model_key,
                "short_prompt": short_prompt, "prompt_file": str(prompt_copy),
                "prepare": outcome.to_dict(),
                "verify": lambda sid="": verify_qoder_session(
                    workspace=workspace, model=request.model,
                    started_at=started_epoch, config=self.config,
                    session_id=sid or str(outcome.evidence.get("session_id") or ""),
                ),
            }
        return {"method": "gui", "fallback_reason": f"{app} 未实现程序接口"}

    def _attach(self, driver: Any, profile: GuiProfile) -> dict[str, Any]:
        if self._driver is not None:
            return driver.attach()
        deadline = time.monotonic() + max(20.0, float(profile.launch_timeout_seconds))
        while time.monotonic() < deadline:
            try:
                if find_window(profile.window_title):
                    return driver.attach()
            except Exception:
                break
            time.sleep(1.0)
        return driver.attach()

    def set_model(self, model_key: str, *, workspace: str = "",
                  log_path: str | Path | None = None) -> dict[str, Any]:
        """Switch the agent's model through its programmatic interface.

        Qoder: main.sqlite + transcript runtime-config (the desktop restores
        the migrated session on restart). WorkBuddy: task deep link (no mouse
        events) followed by the session DB read-back.
        """
        started = time.monotonic()
        profile = self._profile()
        app = str(profile.app or "")
        evidence: dict[str, Any] = {"path": "", "requested": model_key}
        try:
            if app == "qoder_cn":
                key = qoder_model_key(model_key)
                target = workspace or str(self.config.get("model_probe_workspace") or "")
                if not key:
                    raise GuiAutomationError("MODEL_UNAVAILABLE", "模型为空")
                if not target:
                    raise GuiAutomationError(
                        "MODEL_SWITCH_UNSUPPORTED",
                        "Qoder 程序化模型切换需要 workspace（用于迁移会话）",
                    )
                outcome = prepare_qoder_session(workspace=target, model=key,
                                                config=self.config)
                evidence.update({"path": "db+transcript", "actual": key,
                                 "session_id": outcome.evidence.get("session_id", ""),
                                 "evidence": outcome.evidence})
                if not outcome.ok:
                    raise GuiAutomationError(
                        outcome.error_code or "MODEL_SWITCH_UNSUPPORTED",
                        outcome.reason or "Qoder 程序化模型写入失败",
                        details=outcome.to_dict(),
                    )
                evidence["ok"] = True
            elif app == "workbuddy":
                key = workbuddy_model_key(model_key)
                target = workspace or str(self.config.get("model_probe_workspace") or "")
                if not key:
                    raise GuiAutomationError("MODEL_UNAVAILABLE", "模型为空")
                if not target:
                    raise GuiAutomationError(
                        "MODEL_SWITCH_UNSUPPORTED",
                        "WorkBuddy 程序化模型切换需要 workspace",
                    )
                probe = (f"模型切换探针：只回复一行 AGENTMESH_MODEL_PROBE_OK。\n"
                         f"<!-- {MARKER_PREFIX}: {uuid.uuid4().hex} -->\n")
                outcome = prepare_workbuddy_task(
                    workspace=target, model=model_key, prompt=probe,
                    welcome_mode=str(self.config.get("workbuddy_welcome_mode") or "code"),
                    permission_mode=str(self.config.get("workbuddy_permission_mode") or "fullAccess"),
                    config=self.config,
                )
                evidence.update({"path": "deeplink", "actual": key,
                                 "evidence": outcome.evidence})
                if not outcome.ok:
                    raise GuiAutomationError(
                        outcome.error_code or "MODEL_SWITCH_UNSUPPORTED",
                        outcome.reason or "WorkBuddy 深链接模型写入失败",
                        details=outcome.to_dict(),
                    )
                row = None
                deadline = time.time() + 20
                while time.time() < deadline:
                    from ..control.interfaces import _newest_workbuddy_session, workbuddy_db_path
                    row = _newest_workbuddy_session(workbuddy_db_path(self.config),
                                                    started_at=outcome.evidence["started_at"],
                                                    workspace=target)
                    if row and str(row.get("model") or "") == key:
                        break
                    time.sleep(0.5)
                if not row:
                    raise GuiAutomationError("MODEL_SWITCH_UNSUPPORTED",
                                             "WorkBuddy 深链接没有创建会话")
                evidence["actual"] = row.get("model", "")
                evidence["session_id"] = row.get("session_id", "")
                evidence["ok"] = str(row.get("model") or "") == key
                if not evidence["ok"]:
                    raise GuiAutomationError(
                        "MODEL_MISMATCH",
                        f"期望模型 {key}，实际 {row.get('model')}",
                        details=evidence,
                    )
            else:
                raise GuiAutomationError("MODEL_SWITCH_UNSUPPORTED",
                                         f"{app} 没有程序化模型接口")
            evidence["duration_ms"] = int((time.monotonic() - started) * 1000)
            if log_path:
                _log(Path(log_path), "model_switch", **evidence)
            return evidence
        except GuiAutomationError as exc:
            evidence.update({"ok": False, "error_code": exc.code,
                             "reason": exc.message,
                             "duration_ms": int((time.monotonic() - started) * 1000)})
            if log_path:
                _log(Path(log_path), "model_switch", **evidence)
            raise

    def set_model_programmatically(self, model: str) -> dict[str, Any]:
        outcome = set_model_programmatically(self.agent.product or self.agent.agent_id,
                                             model, self.config)
        return outcome.to_dict()

    def health(self) -> dict[str, Any]:
        try:
            profile = self._profile()
        except GuiAutomationError as exc:
            return {"agent_id": self.agent.agent_id, "adapter_type": "gui_automation",
                    "available": False, "error": exc.message}
        exe = next((p for p in profile.executable_candidates if Path(p).exists()), "")
        from ..gui import find_window
        window = find_window(profile.window_title)
        return {
            "agent_id": self.agent.agent_id, "adapter_type": "gui_automation",
            "app": profile.app, "window_title": profile.window_title,
            "executable": exe, "available": bool(exe or window),
            "window_found": bool(window), "status": "online" if (exe or window) else "offline",
        }

    # -- result assembly -------------------------------------------------
    def _finish_success(self, request: ExecutionRequest, reply: GuiReply,
                        profile: GuiProfile, model_key: str, screenshots: list[str],
                        log_path: Path, started: float,
                        interface_info: dict[str, Any] | None = None) -> ExecutionOutcome:
        usage = self._usage(reply, request, profile)
        purpose = str(request.purpose or "task")
        payload = read_json(request.result_path)
        if payload is None:
            if purpose in ("plan", "plan_repair", "verify", "summary"):
                payload = self._extract_json(reply.text)
                if payload is None:
                    raise GuiAutomationError(
                        "GUI_REPLY_PARSE_FAILED",
                        f"{purpose} 回复中未找到可解析的 JSON 对象",
                        details={"transcript": reply.transcript_path,
                                 "chars": len(reply.text)},
                    )
                write_json(request.result_path, payload)
                _log(log_path, "structured_written", path=str(request.result_path),
                     purpose=purpose)
            else:
                payload = self._build_task_result(request, reply, profile, model_key,
                                                  screenshots, usage)
                write_json(request.result_path, payload)
                _log(log_path, "result_built", path=str(request.result_path))
        if purpose == "task":
            normalized, changed = ResultPackage.normalize(payload)
            try:
                ResultPackage.validate(
                    normalized, expected_task_id=request.task.task_id,
                    expected_agent_id=self.agent.agent_id,
                )
            except Exception as exc:
                _log(log_path, "result_invalid", error=str(exc), normalized=changed)
                payload = self._build_task_result(request, reply, profile, model_key,
                                                  screenshots, usage)
                write_json(request.result_path, payload)
        interface_info = interface_info or {"method": "gui"}
        if payload is not None:
            evidence = payload.get("evidence")
            if isinstance(evidence, list):
                evidence.append(
                    f"interface: {interface_info.get('method')} "
                    f"workspace={interface_info.get('prepare', {}).get('expected', {}).get('workspace', '')}"
                )
        return ExecutionOutcome(
            status="completed", text=reply.text[-20000:], usage=usage,
            tool_calls=reply.tool_calls, result_payload=payload,
            result_path=request.result_path, logs=tail_text(log_path, max_chars=30000),
            session_id=reply.session_id, duration_ms=_duration_ms(started),
            extras={"gui_app": profile.app, "window_title": profile.window_title,
                    "model": reply.model, "model_key": model_key,
                    "transcript": reply.transcript_path, "screenshots": screenshots,
                    "events": reply.events, "interface": interface_info},
        )

    def _build_task_result(self, request: ExecutionRequest, reply: GuiReply,
                           profile: GuiProfile, model_key: str, screenshots: list[str],
                           usage: ExecutionUsage) -> dict[str, Any]:
        summary = _clean_summary(reply.text) or f"{profile.product} 已完成任务"
        files_changed = _git_changed(request.workdir)
        commands: list[dict[str, Any]] = []
        for call in reply.tool_calls:
            name = str(call.get("name") or "")
            data = call.get("input") if isinstance(call.get("input"), dict) else {}
            if name in ("Bash", "PowerShell"):
                commands.append({"tool": name, "command": str(
                    data.get("command") or data.get("cmd") or data.get("commandLine") or ""
                )})
        evidence = [
            f"GUI window: {profile.window_title}",
            f"GUI model: {reply.model or profile.display_model}",
            f"GUI session: {reply.session_id}",
            f"GUI transcript: {reply.transcript_path}",
        ] + [f"GUI screenshot: {s}" for s in screenshots if s]
        return {
            "task_id": request.task.task_id,
            "agent_id": self.agent.agent_id,
            "status": "completed",
            "summary": summary,
            "findings": [],
            "files_read": [],
            "files_changed": files_changed,
            "commands_run": commands,
            "tool_calls": reply.tool_calls,
            "tests": _tests_from_text(reply.text),
            "evidence": evidence,
            "risks": [],
            "unresolved_items": [],
            "artifacts": [reply.transcript_path] if reply.transcript_path else [],
            "token_usage": {
                "input": usage.input_tokens, "output": usage.output_tokens,
                "cached": usage.cached_tokens, "reasoning": usage.reasoning_tokens,
                "total": usage.total_tokens, "estimated": usage.estimated,
            },
            "cost_estimate": {
                "amount": round(float(usage.cost), 4), "currency": "credits",
                "estimated": True,
            },
            "recommended_next_action": [],
        }

    def _usage(self, reply: GuiReply, request: ExecutionRequest, profile: GuiProfile) -> ExecutionUsage:
        raw = reply.usage or {}
        input_tokens = int(raw.get("input_tokens", raw.get("inputTokens", 0)) or 0)
        output_tokens = int(raw.get("output_tokens", raw.get("outputTokens", 0)) or 0)
        total = int(raw.get("total_tokens", raw.get("totalTokens", 0)) or 0)
        credits = float(raw.get("credits", 0) or 0)
        estimated = not any((input_tokens, output_tokens, total))
        if not input_tokens:
            input_tokens = estimate_tokens(read_text(request.prompt_path))
        if not output_tokens:
            output_tokens = estimate_tokens(reply.text)
        if not total:
            total = input_tokens + output_tokens
        return ExecutionUsage(
            input_tokens=input_tokens, output_tokens=output_tokens,
            cached_tokens=int(raw.get("cached_tokens", 0) or 0),
            reasoning_tokens=int(raw.get("reasoning_tokens", 0) or 0),
            total_tokens=total, cost=credits, tool_calls=len(reply.tool_calls),
            estimated=estimated,
        ).normalized()

    # -- helpers ---------------------------------------------------------
    def _profile(self) -> GuiProfile:
        app = str(self.config.get("gui_app") or "").strip()
        if not app:
            raise GuiAutomationError("GUI_APP_NOT_CONFIGURED", "adapter_config 缺少 gui_app")
        profile = profile_for_app(app)
        if not profile:
            raise GuiAutomationError("GUI_APP_NOT_CONFIGURED", f"未知 gui_app: {app}")
        return profile

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        candidates: list[str] = []
        for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, re.I):
            candidates.append(match.group(1).strip())
        candidates.append(text.strip())
        for candidate in candidates:
            try:
                value = json.loads(candidate)
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                pass
            start = candidate.find("{")
            while start >= 0:
                depth = 0
                in_string = False
                escape = False
                for index in range(start, len(candidate)):
                    char = candidate[index]
                    if in_string:
                        if escape:
                            escape = False
                        elif char == "\\":
                            escape = True
                        elif char == '"':
                            in_string = False
                        continue
                    if char == '"':
                        in_string = True
                    elif char == "{":
                        depth += 1
                    elif char == "}":
                        depth -= 1
                        if depth == 0:
                            snippet = candidate[start:index + 1]
                            try:
                                value = json.loads(snippet)
                                if isinstance(value, dict):
                                    return value
                            except json.JSONDecodeError:
                                pass
                            break
                start = candidate.find("{", start + 1)
        return None


def _log(log_file: Path, event: str, **data: Any) -> None:
    ensure_dir(Path(log_file).parent)
    with Path(log_file).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": now_iso(), "event": event, **data},
                                ensure_ascii=False) + "\n")


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _clean_summary(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    return cleaned[:500]


def _git_changed(workdir: str) -> list[str]:
    if not workdir or not Path(workdir).exists():
        return []
    try:
        proc = subprocess.run(
            ["git", "-C", workdir, "status", "--porcelain"],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    files: list[str] = []
    for line in proc.stdout.splitlines():
        rel = line[3:].strip() if len(line) > 3 else ""
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1]
        if rel:
            files.append(rel)
    return files[:200]


def _tests_from_text(text: str) -> list[dict[str, Any]]:
    tests: list[dict[str, Any]] = []
    for line in str(text or "").splitlines():
        low = line.lower()
        if ("pytest" in low or "passed" in low) and any(
            token in low for token in ("passed", "failed", "error", "ok")
        ):
            tests.append({"line": line.strip()[:300]})
    return tests[:20]


__all__ = ["GuiAutomationAdapter", "GUI_ERROR_CODES"]
