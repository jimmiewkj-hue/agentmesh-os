"""GUI automation adapter: drive Electron agents through their real UI.

The adapter is capability-based: it only works for agents explicitly registered
as ``gui_automation`` with a known app profile. It sends the control-plane
prompt through the app's window, waits on the agent's own transcript for
completion, then returns a strict structured result package.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from ..gui import (
    GUI_ERROR_CODES, GuiAutomationError, GuiProfile, GuiReply, WindowsGuiDriver,
    create_reader, profile_for_app, resolve_model_key,
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
        try:
            profile = self._profile()
            driver = driver or WindowsGuiDriver(profile, self.config, log_path=log_path)
            reader = self._reader or create_reader(profile, self.config)
            window = driver.attach()
            _log(log_path, "attached", window=window)
            if hasattr(driver, "prepare_new_task"):
                try:
                    if driver.prepare_new_task():
                        _log(log_path, "new_task_ready")
                except Exception as exc:
                    _log(log_path, "new_task_failed", error=str(exc))

            model_key = resolve_model_key(request.model, profile.model_aliases)
            if model_key:
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
            prompt_text = read_text(request.prompt_path)
            if not prompt_text.strip():
                raise GuiAutomationError("GUI_SEND_FAILED", "prompt.md 为空")
            prompt_with_marker = (
                prompt_text
                + "\n\n---\n"
                + f"<!-- {MARKER_PREFIX}: {marker} -->\n"
            )
            driver.send_prompt(prompt_with_marker)
            _log(log_path, "prompt_sent", marker=marker, chars=len(prompt_with_marker))
            timeout = float(request.timeout_seconds or profile.timeout_seconds)
            reply = reader.wait_reply(
                marker, started_at=started_epoch, timeout=timeout,
                stall_seconds=float(self.config.get("stall_seconds") or profile.stall_seconds),
                expected_model=model_key,
            )
            _log(log_path, "reply_received", session_id=reply.session_id,
                 model=reply.model, events=reply.events, chars=len(reply.text))
            shot = driver.screenshot(after_shot)
            if shot:
                screenshots.append(shot)
            return self._finish_success(request, reply, profile, model_key, screenshots,
                                        log_path, started)
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
                        "details": exc.details},
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
                        log_path: Path, started: float) -> ExecutionOutcome:
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
        return ExecutionOutcome(
            status="completed", text=reply.text[-20000:], usage=usage,
            tool_calls=reply.tool_calls, result_payload=payload,
            result_path=request.result_path, logs=tail_text(log_path, max_chars=30000),
            session_id=reply.session_id, duration_ms=_duration_ms(started),
            extras={"gui_app": profile.app, "window_title": profile.window_title,
                    "model": reply.model, "model_key": model_key,
                    "transcript": reply.transcript_path, "screenshots": screenshots,
                    "events": reply.events},
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
