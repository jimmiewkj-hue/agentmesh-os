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
    InterfaceOutcome,
    ensure_workbuddy_cdp, open_workbuddy_chat, prepare_qoder_session,
    prepare_workbuddy_task, qoder_active_session, qoder_app_running,
    qoder_model_key, set_model_programmatically,
    sync_qoder_model_profile, sync_workbuddy_session_profile,
    verify_qoder_session, verify_workbuddy_task, workbuddy_model_key,
    workbuddy_user_id,
)
from ..control import trae_interface
from ..control.zero_input import os_input_allowed
from ..gui.permissions import (
    APP_LABELS, PermissionWatcher, ensure_trae_full_access,
    ensure_workbuddy_full_access,
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
        self.full_control = False
        self.no_gui = (str(self.config.get("no_gui_fallback") or "").lower() in ("1", "true", "yes")
                       or os.environ.get("AGENTMESH_DISABLE_GUI_INPUT") == "1")
        self.qoder_cdp_port = int(
            self.config.get("qoder_cdp_port")
            or os.environ.get("AGENTMESH_QODER_CDP_PORT")
            or 9222
        )
        self.workbuddy_cdp_port = int(
            self.config.get("workbuddy_cdp_port")
            or os.environ.get("AGENTMESH_WORKBUDDY_CDP_PORT")
            or 9223
        )
        self.trae_cdp_port = int(
            self.config.get("trae_cdp_port")
            or os.environ.get("AGENTMESH_TRAE_CDP_PORT")
            or trae_interface.CDP_DEFAULT_PORT
        )
        if str(self.config.get("gui_app") or "") == "qoder_cn":
            # Qoder's prompt path always runs through the renderer DOM, so the
            # restarted desktop must expose DevTools regardless of the legacy
            # no_gui flag.
            os.environ.setdefault("AGENTMESH_QODER_CDP_PORT", str(self.qoder_cdp_port))
        # GUI model clicks are diagnostic only; they require an explicit opt-in
        # even outside zero-mouse mode.
        self.allow_gui_model_fallback = str(
            self.config.get("allow_gui_model_fallback") or ""
        ).lower() in ("1", "true", "yes")
        # Restarting a running desktop app is a user-visible action.  Execution
        # never does it unless the operator explicitly opted in; otherwise the
        # task blocks with APP_RESTART_REQUIRED and the WebUI asks the user.
        self.allow_app_restart = str(
            self.config.get("allow_app_restart")
            or os.environ.get("AGENTMESH_ALLOW_APP_RESTART") or ""
        ).strip().lower() in ("1", "true", "yes", "on")
        self.manual_wait_seconds = int(
            self.config.get("manual_wait_seconds")
            or os.environ.get("AGENTMESH_MANUAL_WAIT_SECONDS")
            or 1800
        )

    # -- public contract -------------------------------------------------
    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        started = time.monotonic()
        gui_dir = ensure_dir(Path(request.task_dir) / "gui")
        log_path = gui_dir / "gui.log"
        before_shot = gui_dir / "before.png"
        after_shot = gui_dir / "after.png"
        started_epoch = time.time() - 1
        marker = str((getattr(request, "metadata", {}) or {}).get("request_marker")
                     or f"{MARKER_PREFIX}_{uuid.uuid4().hex}")
        screenshots: list[str] = []
        reply: GuiReply | None = None
        driver = self._driver
        watcher: PermissionWatcher | None = None
        self.full_control = bool(getattr(request, "full_control", True))
        interface_info: dict[str, Any] = {"method": "gui"}
        sent = False
        receipt: dict[str, Any] = {}
        manual: dict[str, Any] | None = None
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
            if hasattr(reader, "cancel_event"):
                reader.cancel_event = request.cancel_event
            if profile.app == "workbuddy" and hasattr(reader, "cdp_port"):
                reader.cdp_port = self.workbuddy_cdp_port
            if hasattr(reader, "completion_path"):
                # GUI planning/task result files are unique per request; a
                # complete JSON file is an unambiguous done signal even when
                # Trae reuses a session whose messageIds no longer grow.
                reader.completion_path = str(request.result_path)
            interface_info = self._prepare_interface(
                profile, request, prompt_with_marker, marker, log_path, started_epoch,
            )
            interface_info["full_control"] = self.full_control
            if self.full_control:
                full_access: dict[str, Any] = {}
                if profile.app == "trae_cn" or profile.app == "trae":
                    full_access = ensure_trae_full_access(
                        port=self.trae_cdp_port, log_path=log_path)
                if full_access:
                    _log(log_path, "full_access", app=profile.app, **full_access)
                    interface_info["full_access"] = full_access
                if profile.app in APP_LABELS:
                    port = {
                        "trae_cn": self.trae_cdp_port, "trae": self.trae_cdp_port,
                        "workbuddy": self.workbuddy_cdp_port,
                        "qoder_cn": self.qoder_cdp_port,
                    }.get(profile.app, 0)
                    page_id = str(interface_info.get("trae_page_id")
                                  or interface_info.get("cdp_target") or "")
                    if port and not page_id and profile.app == "workbuddy":
                        try:
                            from ..gui.cdp import _pick_page, _targets
                            page = _pick_page(_targets(port))
                            page_id = str((page or {}).get("id") or "")
                        except Exception:
                            page_id = ""
                    if port and page_id:
                        watcher = PermissionWatcher(
                            app=profile.app, port=port, page_id=page_id,
                            log_path=log_path,
                        ).start()
                        interface_info["permission_watcher"] = "started"
                        interface_info["permission_target"] = page_id
                    else:
                        # Auto-accepting a dialog without a proven target would
                        # click whatever page happens to be first.  Wait for
                        # the user instead.
                        interface_info["permission_watcher"] = "skipped_no_target"
                        _log(log_path, "permission_watcher_skipped",
                             app=profile.app, port=port, page_id=page_id)
            window = self._attach(driver, profile)
            try:
                from ..gui.windows import window_process_identity
                hwnd = int((window or {}).get("hwnd") or 0) if isinstance(window, dict) else 0
                identity = window_process_identity(hwnd)
            except Exception:
                identity = {}
            interface_info["window"] = window
            interface_info["process"] = identity
            _log(log_path, "attached", window=window, process=identity,
                 method=interface_info.get("method"))
            if interface_info.get("method") == "qoder_db_transcript":
                expected = str(((interface_info.get("prepare") or {}).get(
                    "evidence") or {}).get("session_id") or "")
                if expected:
                    current = str(qoder_active_session(self.config).get(
                        "session_id") or "")
                    if current and current != expected:
                        # Never type a run prompt into the user's own chat.
                        raise GuiAutomationError(
                            "WORKSPACE_MISMATCH",
                            f"Qoder 没有打开本次独立会话（当前 {current}，期望 {expected}）",
                            details={"expected": expected, "current": current},
                        )
            model_key = interface_info.get("model_key") or \
                resolve_model_key(request.model, profile.model_aliases)

            if interface_info.get("method") == "qoder_db_transcript" and model_key:
                # Qoder ignores the session row's model when it opens a fresh
                # chat; pick the model in its own menu before typing.
                from ..gui.cdp import select_qoder_model_via_cdp
                try:
                    picked = select_qoder_model_via_cdp(
                        model_key, port=self.qoder_cdp_port,
                        page_id=str(interface_info.get("cdp_target") or ""),
                    )
                except Exception as exc:
                    picked = {"ok": False, "reason": str(exc)[:300]}
                interface_info["model_cdp"] = picked
                _log(log_path, "model_cdp_select", model=model_key, **picked)
                if not picked.get("ok"):
                    raise GuiAutomationError(
                        "GUI_MODEL_SWITCH_FAILED",
                        f"Qoder 未能切换到模型 {model_key}: "
                        f"{picked.get('reason') or picked}",
                        details=picked,
                    )

            if interface_info.get("method") == "qoder_db_transcript":
                # prepare_qoder_session already wrote the model into main.sqlite
                # and the transcript runtime-config; the desktop restores that
                # session on restart, so neither the workspace nor the model
                # needs a GUI click.  Wait for the renderer's DevTools endpoint
                # instead of a fixed boot sleep: the composer only exists once
                # the page is ready.
                from ..gui.cdp import wait_for_cdp
                ready = wait_for_cdp(
                    self.qoder_cdp_port,
                    timeout=float(self.config.get("qoder_cdp_timeout_seconds") or 60),
                )
                interface_info["cdp_port"] = self.qoder_cdp_port
                interface_info["cdp_target"] = ready.get("page", {}).get("id", "")
                _log(log_path, "model_programmatic", model=model_key,
                     path="db+transcript", cdp="ready",
                     target=interface_info["cdp_target"])
            if interface_info.get("method") == "qoder_db_transcript" and model_key \
                    and self.allow_gui_model_fallback and os_input_allowed():
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
                if self._driver is None and not os_input_allowed():
                    raise GuiAutomationError(
                        "GUI_INPUT_FORBIDDEN",
                        "零 OS 输入策略禁止通用 GUI 主链：该 Agent 没有可用的进程内接口",
                    )
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
                    if self.no_gui or not self.allow_gui_model_fallback \
                            or (self._driver is None and not os_input_allowed()):
                        raise GuiAutomationError(
                            "MODEL_GUI_FORBIDDEN",
                            "GUI 模型点击未显式启用（allow_gui_model_fallback）",
                        )
                    _log(log_path, "model_gui_fallback", model=model_key,
                         reason="legacy gui path")
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
            cdp_result, manual = self._submit_prompt(
                request, interface_info, marker, prompt_to_send, log_path, profile,
                reader=reader, started_at=started_epoch)
            sent = manual is None
            receipt = self._submission_receipt(
                reader, marker, started_epoch, cdp_result, manual, profile)
            interface_info["submission"] = receipt
            _log(log_path, "submission_receipt", **receipt)
            initial_sync = interface_info.get("profile_sync") or {}
            if profile.app == "workbuddy" and \
                    (initial_sync.get("evidence") or {}).get("deferred"):
                # The session row is created with the first prompt.  Persist
                # the requested profile now so WorkBuddy's next turn/session
                # reads the same context/effort the user selected.
                prepare = interface_info.get("prepare") or {}
                deferred_sync = sync_workbuddy_session_profile(
                    workspace=str(request.workdir),
                    started_at=float((prepare.get("evidence") or {}).get(
                        "started_at") or started_epoch),
                    model=request.model,
                    context_length=int(getattr(request, "context_length", 0) or 0),
                    capability_level=str(
                        getattr(request, "capability_level", "") or ""),
                    config=self.config, timeout_seconds=30.0,
                )
                interface_info["profile_sync_after_submit"] = \
                    deferred_sync.to_dict()
                _log(log_path, "profile_sync_after_submit", app=profile.app,
                     **deferred_sync.to_dict())
            if cdp_result.get("page_id") and hasattr(reader, "page_id"):
                # Poll the exact renderer that received and submitted the
                # prompt; Trae may have several windows open.
                reader.page_id = str(cdp_result["page_id"])
                interface_info["trae_page_id"] = reader.page_id
            timeout = float(request.timeout_seconds or profile.timeout_seconds)
            if manual is not None:
                # Human submit/permission waits use their own deadline; they
                # must not consume the ordinary execution timeout.
                timeout = max(timeout, float(self.manual_wait_seconds))
            # Never stall earlier than the measured profile threshold; the
            # discovery-written adapter_config may still carry an old 90s.
            stall_seconds = max(float(self.config.get("stall_seconds") or 0),
                                float(profile.stall_seconds))
            if profile.app == "workbuddy":
                # A legitimate npm install / Blender asset build can run for
                # several minutes without emitting a transcript event.
                stall_seconds = max(
                    stall_seconds,
                    float(self.config.get("workbuddy_stall_seconds") or 600),
                )
            if manual is not None and hasattr(reader, "send_timeout"):
                # The prompt is prefilled but the app kept its send button
                # disabled.  The user was just asked to click it by hand, so
                # give the transcript reader the same manual window instead of
                # abandoning the prompt after the normal 90s.
                reader.send_timeout = max(
                    float(getattr(reader, "send_timeout", 0) or 0),
                    float(self.manual_wait_seconds),
                )
            reply = reader.wait_reply(
                marker, started_at=started_epoch, timeout=timeout,
                stall_seconds=stall_seconds, expected_model=model_key,
            )
            _log(log_path, "reply_received", session_id=reply.session_id,
                 model=reply.model, events=reply.events, chars=len(reply.text))
            receipt["reply_observed"] = True
            receipt["session_id"] = reply.session_id
            interface_info["submission"] = receipt
            if manual is not None:
                self._end_manual(request, log_path, outcome="resolved",
                                 detail={"session_id": reply.session_id})
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
            manual_timeout = manual is not None and exc.code in ("GUI_TIMEOUT", "GUI_STALLED")
            if manual is not None and not manual_timeout:
                self._end_manual(request, log_path, outcome="failed",
                                 detail={"error": f"{exc.code}: {exc.message}"})
            if driver:
                try:
                    shot = driver.screenshot(gui_dir / "error.png")
                    if shot:
                        screenshots.append(shot)
                except Exception:
                    pass
            _log(log_path, "error", code=exc.code, message=exc.message,
                 details=exc.details, screenshots=screenshots)
            app = str(profile.app if 'profile' in locals() else "")
            reader = locals().get("reader")
            if (app == "workbuddy" and exc.code == "GUI_TIMEOUT"
                    and manual is None and reader is not None
                    and hasattr(reader, "active_sessions")):
                try:
                    active = reader.active_sessions(
                        max_idle_seconds=float(self.config.get(
                            "workbuddy_active_idle_seconds") or 900))
                except Exception:
                    active = []

                def _norm_path(value: str) -> str:
                    return str(value or "").replace("\\", "/").rstrip("/").lower()

                same_task = [
                    item for item in active
                    if _norm_path(item.get("cwd") or "") ==
                    _norm_path(str(request.workdir or ""))
                ]
                if same_task:
                    message = (
                        f"WorkBuddy 已超过 {request.timeout_seconds or 0:.0f}s 仍在执行该节点，"
                        "控制平面已停止等待但不会重试或新开会话"
                    )
                    manual = self._begin_manual(
                        request, log_path, kind="agent_busy", app=app,
                        message=message,
                        instructions=(
                            "WorkBuddy 超时后会话仍在更新，说明模型/工具仍在工作。"
                            "请等待应用内完成：若已写出 result.json，点“重新验证”；"
                            "若没有成果包，点“重试该节点”。未处理前不会自动重试，"
                            "也不会新开第二个 WorkBuddy 会话。"
                        ),
                        error_code=exc.code,
                        extra={"details": exc.details,
                               "sessions": same_task[:3],
                               "prompt_file": interface_info.get("prompt_file", "")},
                    )
                    return ExecutionOutcome(
                        status="waiting_user",
                        error=f"MANUAL_ACTION_REQUIRED: {message}",
                        error_code=exc.code,
                        logs=tail_text(log_path, max_chars=20000),
                        duration_ms=_duration_ms(started),
                        extras={"gui_app": app, "screenshots": screenshots,
                                "error_code": exc.code, "details": exc.details,
                                "manual_action": manual,
                                "still_running": same_task[:3],
                                "interface": {k: ("<callable>" if callable(v) else v)
                                              for k, v in interface_info.items()}},
                        result_path=request.result_path,
                        submission_status=str(receipt.get("status") or ""),
                        submission_receipt=receipt,
                    )
            blocking_codes = {
                "APP_RESTART_REQUIRED", "GUI_SEND_FAILED", "GUI_DRIVER_ERROR",
                "WORKSPACE_SWITCH_FAILED", "WORKSPACE_MISMATCH",
                "WORKSPACE_TRUST_REQUIRED",
                "INTERFACE_WRITE_FAILED", "MODEL_UNAVAILABLE", "MODEL_MISMATCH",
                "GUI_MODEL_SWITCH_FAILED", "GUI_MODE_MISMATCH",
                "MODEL_GUI_FORBIDDEN", "GUI_INPUT_FORBIDDEN", "APP_BUSY",
                "APP_LOGIN_REQUIRED",
            }
            # Injected drivers/readers are test diagnostics; a real desktop
            # interface attempt is required before the chain blocks for a human.
            if (not sent and app and exc.code in blocking_codes
                    and self._driver is None and self._reader is None):
                if exc.code == "APP_RESTART_REQUIRED":
                    kind = "restart_required"
                    instructions = (
                        f"{app} 正在运行但没有开放程序接口。请点击“重启 Agent 接口”"
                        "由控制平面优雅重启它，或手动完全退出该应用后点击“继续运行”。"
                    )
                elif exc.code == "APP_BUSY":
                    kind = "agent_busy"
                    same_task = bool((exc.details or {}).get("same_task"))
                    if same_task:
                        instructions = (
                            "该节点自己的上一会话仍在 WorkBuddy 中运行（模型可能仍在深度思考）。"
                            "请等待它结束，或先在该会话点击停止按钮，然后点击“重试该节点”。"
                            "未处理前不会新开第二个会话。"
                        )
                    else:
                        instructions = (
                            f"{app} 还有其他任务在运行。请先在 {app} 中等待它完成，"
                            "或点击该任务的停止按钮，然后点击“重试该节点”。"
                            "未处理前不会新开第二个任务，避免两个会话互相覆盖。"
                        )
                elif exc.code == "APP_LOGIN_REQUIRED":
                    kind = "login_required"
                    instructions = (
                        f"{app} 当前未登录，AI 模型不可用。请切换到 {app} 完成登录，"
                        "确认输入框可正常使用后，点击“重试该节点”。"
                    )
                elif exc.code == "WORKSPACE_TRUST_REQUIRED":
                    kind = "workspace_trust"
                    instructions = (
                        f"{app} 弹出工作区信任对话框且无法自动确认。请在应用内点击"
                        "“是，我信任此作者”，然后点击“重试该节点”；"
                        "控制平面在下次派发前也会再次尝试自动确认。"
                    )
                else:
                    kind = "agent_not_running"
                    instructions = (
                        f"请切换到 {app} 检查任务是否已发出；若输入框仍在，请手动点击发送"
                        "或完成应用内审批。该节点完成后链路会自动继续。"
                    )
                manual = self._begin_manual(
                    request, log_path, kind=kind, app=app,
                    message=exc.message, instructions=instructions,
                    error_code=exc.code,
                    extra={"details": exc.details,
                           "prompt_file": interface_info.get("prompt_file", "")},
                )
                return ExecutionOutcome(
                    status="waiting_user",
                    error=f"MANUAL_ACTION_REQUIRED: {exc.message}",
                    error_code=exc.code,
                    logs=tail_text(log_path, max_chars=20000),
                    duration_ms=_duration_ms(started),
                    extras={"gui_app": app, "screenshots": screenshots,
                            "error_code": exc.code, "details": exc.details,
                            "manual_action": manual,
                            "interface": {k: ("<callable>" if callable(v) else v)
                                          for k, v in interface_info.items()}},
                    result_path=request.result_path,
                    submission_status=str(receipt.get("status") or ""),
                    submission_receipt=receipt,
                )
            if manual_timeout:
                message = (
                    f"等待人工操作超过 {self.manual_wait_seconds}s，节点保持阻塞；"
                    "不会自动重试或新建尝试"
                )
                manual = self._begin_manual(
                    request, log_path, kind="manual_wait_timeout", app=app,
                    message=message,
                    instructions=(
                        "请切换到 Agent 应用完成发送/审批；完成后点击“继续运行”，"
                        "控制平面会先核对原请求再继续。未处理前不会重新发送。"
                    ),
                    error_code=exc.code,
                    extra={"details": exc.details,
                           "prompt_file": interface_info.get("prompt_file", ""),
                           "original_manual": manual},
                )
                return ExecutionOutcome(
                    status="waiting_user",
                    error=f"MANUAL_ACTION_OVERDUE: {message}",
                    error_code=exc.code,
                    logs=tail_text(log_path, max_chars=20000),
                    duration_ms=_duration_ms(started),
                    extras={"gui_app": app, "screenshots": screenshots,
                            "error_code": exc.code, "details": exc.details,
                            "manual_action": manual,
                            "interface": {k: ("<callable>" if callable(v) else v)
                                          for k, v in interface_info.items()}},
                    result_path=request.result_path,
                    submission_status=str(receipt.get("status") or ""),
                    submission_receipt=receipt,
                )
            status = "timeout" if exc.code in ("GUI_TIMEOUT", "GUI_STALLED") else "failed"
            return ExecutionOutcome(
                status=status, error=f"{exc.code}: {exc.message}", error_code=exc.code,
                logs=tail_text(log_path, max_chars=20000),
                duration_ms=_duration_ms(started), session_id=(reply.session_id if reply else ""),
                extras={"gui_app": app,
                        "screenshots": screenshots, "error_code": exc.code,
                        "details": exc.details,
                        "interface": {k: ("<callable>" if callable(v) else v)
                                      for k, v in interface_info.items()}},
                text=(reply.text if reply else ""),
                result_payload=None, result_path=request.result_path,
                submission_status=str(receipt.get("status") or ""),
                submission_receipt=receipt,
            )
        except Exception as exc:  # unexpected driver/parse failure must stay diagnosable
            _log(log_path, "error", code="GUI_DRIVER_ERROR", message=str(exc))
            return ExecutionOutcome(
                status="failed", error=f"GUI_DRIVER_ERROR: {exc}", error_code="GUI_DRIVER_ERROR",
                logs=tail_text(log_path, max_chars=20000),
                duration_ms=_duration_ms(started),
                extras={"screenshots": screenshots},
                submission_status=str(receipt.get("status") or ""),
                submission_receipt=receipt,
            )
        finally:
            if watcher is not None:
                try:
                    watcher.stop()
                    interface_info["permission_accepted"] = len(watcher.accepted)
                except Exception:
                    pass
            if driver:
                try:
                    driver.close()
                except Exception:
                    pass

    # -- manual intervention bridge ---------------------------------------
    def _notify(self, request: ExecutionRequest, event: str,
                payload: dict[str, Any]) -> None:
        callback = getattr(request, "notify", None)
        if not callable(callback):
            return
        try:
            callback({"event": event, **payload})
        except Exception:
            # The control plane must never lose the adapter's actual result
            # because a UI notification failed.
            pass

    def _begin_manual(self, request: ExecutionRequest, log_path: Path, *,
                      kind: str, app: str, message: str, instructions: str,
                      error_code: str = "",
                      extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "kind": kind,
            "app": app,
            "agent_id": self.agent.agent_id,
            "agent_name": self.agent.name,
            "model": request.model,
            "task_uid": request.task.uid,
            "task_id": request.task.task_id,
            "message": message,
            "instructions": instructions,
            "error_code": error_code,
            "prompt_path": request.prompt_path,
            "result_path": request.result_path,
            "since": now_iso(),
            "timeout_seconds": self.manual_wait_seconds,
            **(extra or {}),
        }
        target = ensure_dir(Path(request.task_dir)) / "manual-action.json"
        write_json(target, payload)
        _log(log_path, "manual_action_required", **payload)
        self._notify(request, "manual_action_required", payload)
        return payload

    def _end_manual(self, request: ExecutionRequest, log_path: Path, *,
                    outcome: str, detail: dict[str, Any] | None = None) -> None:
        target = Path(request.task_dir) / "manual-action.json"
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        payload = {
            "task_uid": request.task.uid,
            "task_id": request.task.task_id,
            "agent_id": self.agent.agent_id,
            "outcome": outcome,
            "detail": detail or {},
        }
        _log(log_path, "manual_action_resolved" if outcome == "resolved"
             else "manual_action_failed", **payload)
        self._notify(request, "manual_action_resolved" if outcome == "resolved"
                     else "manual_action_failed", payload)

    def _marker_already_sent(self, reader: Any, marker: str,
                             started_at: float) -> bool:
        checker = getattr(reader, "has_marker", None)
        if not callable(checker):
            return False
        try:
            return bool((checker(marker, started_at=started_at) or {}).get("ok"))
        except Exception:
            return False

    def _submit_prompt(self, request: ExecutionRequest,
                       interface_info: dict[str, Any], marker: str,
                       prompt_to_send: str, log_path: Path,
                       profile: GuiProfile, *, reader: Any = None,
                       started_at: float = 0.0
                       ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Submit through the app interface; on failure, ask the user.

        Restarting WorkBuddy/Trae here was the direct cause of the observed
        "闪退": a prefill race made the old code kill three perfectly healthy
        processes in 20 seconds.  This path never restarts.  It re-issues the
        deeplink once, falls back to inserting into the visible composer, and
        then blocks on a manual action file that the WebUI surfaces.
        """
        method = str(interface_info.get("method") or "")
        if method == "workbuddy_resume_chat":
            from ..gui.cdp import send_workbuddy_conversation_prompt
            session_id = str(interface_info.get("resumed_session_id") or "")
            result = send_workbuddy_conversation_prompt(
                session_id, prompt_to_send, port=self.workbuddy_cdp_port,
                page_id=str(interface_info.get("cdp_target") or ""),
                timeout=30,
            )
            _log(log_path, "prompt_submitted", marker=marker,
                 chars=len(prompt_to_send), method="workbuddy-resume-prepare-input",
                 cdp=result, session_id=session_id)
            if not result.get("ok"):
                raise GuiAutomationError(
                    "GUI_SEND_FAILED",
                    f"WorkBuddy 续接会话发送失败: {result.get('reason') or result}",
                    details={"session_id": session_id, "result": result},
                )
            return result, None
        if method == "workbuddy_deeplink":
            from ..gui.cdp import send_visible_prompt_via_cdp, submit_composer_via_cdp
            errors: list[str] = []
            for attempt in (1, 2):
                if reader is not None and self._marker_already_sent(
                        reader, marker, started_at):
                    _log(log_path, "submit_already_delivered", marker=marker,
                         before="deeplink_attempt", attempt=attempt)
                    return {"ok": True, "already_delivered": True}, None
                try:
                    result = submit_composer_via_cdp(
                        port=self.workbuddy_cdp_port,
                        prefill_timeout=float(
                            self.config.get("workbuddy_prefill_timeout_seconds") or 20
                        ),
                        submit_retries=int(
                            self.config.get("workbuddy_submit_retries") or 30
                        ),
                        submit_interval=0.5,
                        expect_contains=marker,
                        page_id=str(interface_info.get("cdp_target") or ""),
                    )
                    _log(log_path, "prompt_submitted", marker=marker,
                         chars=len(prompt_to_send), method="deeplink+cdp-dom",
                         cdp=result, deeplink=interface_info.get("deeplink", ""))
                    return result, None
                except GuiAutomationError as exc:
                    errors.append(f"{exc.code}: {exc.message}")
                    _log(log_path, "submit_retry_deeplink", attempt=attempt,
                         error=f"{exc.code}: {exc.message}")
                    if attempt == 1:
                        # Re-issue the same deeplink; it calls prepareInput and
                        # never sends, and does not touch the process.
                        prepare_workbuddy_task(
                            workspace=str(interface_info.get("workspace") or ""),
                            model=request.model, prompt=prompt_to_send,
                            welcome_mode=str(interface_info.get("welcome_mode") or "code"),
                            permission_mode=str(interface_info.get("permission_mode")
                                                or "fullAccess"),
                            config=self.config,
                        )
                        time.sleep(float(
                            self.config.get("workbuddy_deeplink_settle_seconds") or 3.0
                        ))
            if reader is not None and self._marker_already_sent(
                    reader, marker, started_at):
                _log(log_path, "submit_already_delivered", marker=marker,
                     before="visible_composer")
                return {"ok": True, "already_delivered": True}, None
            try:
                result = send_visible_prompt_via_cdp(
                    prompt_to_send, port=self.workbuddy_cdp_port, timeout=15,
                    submit_retries=int(self.config.get("workbuddy_submit_retries") or 30),
                    submit_interval=0.5,
                    page_id=str(interface_info.get("cdp_target") or ""),
                )
                _log(log_path, "prompt_submitted", marker=marker,
                     chars=len(prompt_to_send), method="cdp-visible-composer",
                     cdp=result, previous_errors=errors)
                return result, None
            except GuiAutomationError as exc:
                errors.append(f"{exc.code}: {exc.message}")
                _log(log_path, "prompt_insert_fallback_failed",
                     error=f"{exc.code}: {exc.message}")
            manual = self._begin_manual(
                request, log_path, kind="manual_submit",
                app=str(profile.app or "workbuddy"),
                message="WorkBuddy 输入框未自动提交，需要人工点击发送",
                instructions=(
                    "WorkBuddy 已打开任务（或需要重新粘贴任务说明）。请切换到 WorkBuddy，"
                    f"确认输入框内容后点击“发送”；任务说明文件：{request.prompt_path}。"
                    "发送完成后链路会自动继续。"
                ),
                error_code="GUI_SEND_FAILED",
                extra={"errors": errors, "marker": marker,
                       "prompt_file": interface_info.get("prompt_file", "")},
            )
            return {"ok": False, "manual": True, "errors": errors}, manual
        if method == "qoder_db_transcript":
            from ..gui.cdp import send_prompt_via_cdp
            try:
                result = send_prompt_via_cdp(
                    prompt_to_send, port=self.qoder_cdp_port,
                    page_id=str(interface_info.get("cdp_target") or ""))
            except GuiAutomationError as exc:
                manual = self._begin_manual(
                    request, log_path, kind="manual_submit",
                    app=str(profile.app or "qoder_cn"),
                    message=f"Qoder 自动提交失败: {exc.message}",
                    instructions=(
                        "请切换到 Qoder，确认任务文本并在输入框点击“发送”；"
                        f"任务说明文件：{request.prompt_path}。发送完成后链路会自动继续。"
                    ),
                    error_code=exc.code,
                    extra={"marker": marker,
                           "prompt_file": interface_info.get("prompt_file", "")},
                )
                return {"ok": False, "manual": True}, manual
            _log(log_path, "prompt_sent", marker=marker, chars=len(prompt_to_send),
                 method=method, input="cdp-dom", cdp=result)
            return result, None
        if method == "trae_state_vscdb":
            try:
                result = trae_interface.send_trae_prompt(
                    prompt_to_send, port=self.trae_cdp_port,
                    page_id=str(interface_info.get("trae_page_id") or ""),
                )
            except GuiAutomationError as exc:
                manual = self._begin_manual(
                    request, log_path, kind="manual_submit",
                    app=str(profile.app or "trae_cn"),
                    message=f"Trae 自动发送失败: {exc.message}",
                    instructions=(
                        "请切换到 Trae，任务文本通常已在输入框；确认后点击应用内的发送按钮"
                        f"（或完成应用内审批）。任务说明文件：{request.prompt_path}。"
                        "发送完成后链路会自动继续。"
                    ),
                    error_code=exc.code,
                    extra={"marker": marker,
                           "prompt_file": interface_info.get("prompt_file", "")},
                )
                return {"ok": False, "manual": True}, manual
            _log(log_path, "prompt_sent", marker=marker, chars=len(prompt_to_send),
                 method="trae_state_vscdb", input="cdp-dom", cdp=result)
            return result, None
        driver = self._driver
        if driver is None:
            raise GuiAutomationError("GUI_DRIVER_ERROR", "GUI driver 不可用")
        driver.send_prompt(prompt_to_send)
        _log(log_path, "prompt_sent", marker=marker, chars=len(prompt_to_send),
             method=method, input="diagnostic-gui")
        return {}, None

    def _submission_receipt(self, reader: Any, marker: str, started_at: float,
                            cdp_result: dict[str, Any] | None,
                            manual: dict[str, Any] | None,
                            profile: GuiProfile) -> dict[str, Any]:
        """Observe acceptance separately from clicking a send button."""
        base = {"marker": marker, "method": "gui", "at": now_iso()}
        if manual is not None:
            return {**base, "status": "not_sent",
                    "reason": "manual_action_required"}
        if not isinstance(cdp_result, dict) or not cdp_result.get("ok"):
            return {**base, "status": "not_sent",
                    "reason": str((cdp_result or {}).get("reason") or "submit_failed")}
        checker = getattr(reader, "has_marker", None)
        if not callable(checker):
            return {**base, "status": "acceptance_unknown",
                    "reason": "NO_TRANSCRIPT_RECEIPT"}
        deadline = time.monotonic() + float(
            self.config.get("submission_receipt_timeout_seconds") or 6)
        last: dict[str, Any] = {"ok": False, "reason": "MARKER_NOT_FOUND"}
        while time.monotonic() < deadline:
            try:
                last = checker(marker, started_at=started_at) or last
            except Exception as exc:
                last = {"ok": False, "reason": str(exc)[:200]}
            if last.get("ok"):
                return {**base, "status": "accepted",
                        "reason": "marker_in_transcript",
                        "evidence": last}
            time.sleep(0.5)
        return {**base, "status": "acceptance_unknown",
                "reason": str(last.get("reason") or "MARKER_NOT_FOUND"),
                "evidence": last}

    def reconcile_submission(self, attempt: dict[str, Any]) -> dict[str, Any]:
        """Re-read only the original request marker; never resend the prompt."""
        binding = dict(attempt.get("binding") or {})
        submission = binding.get("submission") if isinstance(
            binding.get("submission"), dict) else {}
        marker = str(binding.get("marker") or submission.get("marker") or "")
        if not marker:
            return {"status": "acceptance_unknown", "reason": "NO_REQUEST_MARKER",
                    "reconciled_at": now_iso()}
        profile = self._profile()
        reader = self._reader or create_reader(profile, self.config)
        checker = getattr(reader, "has_marker", None)
        if not callable(checker):
            return {"status": "acceptance_unknown", "reason": "NO_TRANSCRIPT_RECEIPT",
                    "marker": marker, "reconciled_at": now_iso()}
        result_path = str(attempt.get("result_path") or "")
        if hasattr(reader, "completion_path") and result_path:
            # The attempt-specific result path is disclosed only in the
            # delivered prompt; readers may use it as delivery evidence.
            reader.completion_path = result_path
        try:
            found = checker(marker, started_at=0)
        except Exception as exc:
            return {"status": "acceptance_unknown", "reason": str(exc)[:300],
                    "marker": marker, "reconciled_at": now_iso()}
        receipt = {"marker": marker, "reconciled_at": now_iso(),
                   "evidence": found,
                   "status": "accepted" if found.get("ok") else "acceptance_unknown"}
        if not found.get("ok"):
            receipt["reason"] = str(found.get("reason") or "MARKER_NOT_FOUND")
        package = None
        if result_path:
            loaded = read_json(result_path)
            if isinstance(loaded, dict) and loaded:
                package = loaded
                receipt["result_path"] = result_path
        if package is not None:
            receipt["package"] = package
        return receipt

    # -- programmatic interface -------------------------------------------
    def _prepare_interface(self, profile: GuiProfile, request: ExecutionRequest,
                           prompt_with_marker: str, marker: str,
                           log_path: Path, started_epoch: float) -> dict[str, Any]:
        """Use the agent's own launch/store interface before touching the GUI."""
        app = str(profile.app or "")
        workspace = str(request.workdir or "")
        plan_hint = ""
        if request.purpose in ("plan", "plan_repair"):
            plan_hint = (
                "本步骤只做规划，不要创建实现文件；最终回复必须包含完整 JSON 代码块，"
                f"并把同一 JSON 写入 `{request.result_path}`。"
            )
        if self._driver is not None or self._reader is not None:
            return {"method": "gui", "fallback_reason": "injected stub driver/reader"}
        if not workspace:
            return {"method": "gui", "fallback_reason": "no workspace"}
        if app == "workbuddy":
            model_key = workbuddy_model_key(request.model)
            welcome = str(self.config.get("workbuddy_welcome_mode") or "code")
            permission = str(self.config.get("workbuddy_permission_mode") or "fullAccess")
            if not self.full_control:
                # "完全控制" is off: never request fullAccess for this session.
                # If the app's own default is still broader, the post-run
                # readback blocks the task instead of silently accepting it.
                permission = ""
            prompt_dir = ensure_dir(Path(workspace) / ".agentmesh")
            prompt_copy = prompt_dir / "interface-prompt.md"
            atomic_write_text(prompt_copy, prompt_with_marker)
            short_prompt = (
                f"请先读取并严格执行 `{prompt_copy}` 中的任务说明，"
                "完成后按其中的输出契约写结果。\n"
                f"{plan_hint}\n"
                f"<!-- {MARKER_PREFIX}: {marker} -->\n"
            )
            guard = self._reader
            if guard is None:
                guard = create_reader(profile, self.config)
            if hasattr(guard, "active_sessions"):
                active = guard.active_sessions(
                    max_idle_seconds=float(
                        self.config.get("workbuddy_active_idle_seconds") or 900
                    )
                )
                executable = str(self.config.get("executable") or "")
                if active and executable:
                    from ..control.apps import process_running
                    if not process_running(Path(executable).name):
                        # The app was killed mid-task; its DB still says
                        # "working".  That row is not a live task and must not
                        # block the session's own resume.
                        _log(log_path, "workbuddy_stale_session_ignored",
                             executable=executable,
                             sessions=[{k: item.get(k) for k in
                                        ("id", "cwd", "status", "last_activity_at")}
                                       for item in active[:5]])
                        active = []
                if active:
                    wait_seconds = float(
                        self.config.get("workbuddy_active_wait_seconds") or 120
                    )
                    _log(log_path, "workbuddy_active_wait",
                         sessions=[{k: item.get(k) for k in
                                    ("id", "cwd", "title", "status", "last_activity_at")}
                                   for item in active[:5]],
                         timeout_seconds=wait_seconds)
                    deadline = time.monotonic() + max(0.0, wait_seconds)
                    while active and time.monotonic() < deadline:
                        time.sleep(2.0)
                        active = guard.active_sessions(
                            max_idle_seconds=float(
                                self.config.get("workbuddy_active_idle_seconds") or 900
                            )
                        )
                    if active:
                        def _norm(value: str) -> str:
                            return str(value or "").replace("\\", "/").rstrip("/").lower()
                        same_task = any(
                            _norm(item.get("cwd") or "") == _norm(workspace)
                            for item in active
                        )
                        _log(log_path, "workbuddy_busy", same_task=same_task,
                             sessions=[{k: item.get(k) for k in
                                        ("id", "cwd", "title", "status", "last_activity_at")}
                                       for item in active[:5]])
                        message = (
                            "该节点自己的上一 WorkBuddy 会话仍在运行；"
                            "请等待它完成或先在 WorkBuddy 中停止，再重试"
                            if same_task else
                            "WorkBuddy 还有另一个任务在运行；请等待或先停止它，再重试"
                        )
                        raise GuiAutomationError(
                            "APP_BUSY",
                            message,
                            details={"same_task": same_task, "sessions": [
                                {k: item.get(k) for k in
                                 ("id", "cwd", "title", "status", "last_activity_at")}
                                for item in active[:5]
                            ]},
                        )
            cdp = ensure_workbuddy_cdp(
                executable=str(self.config.get("executable") or ""),
                port=self.workbuddy_cdp_port,
                allow_restart=self.allow_app_restart,
            )
            _log(log_path, "cdp_ready", app=app, **cdp.to_dict())
            if not cdp.ok:
                raise GuiAutomationError(
                    cdp.error_code or "GUI_SEND_FAILED",
                    cdp.reason or "WorkBuddy CDP 未就绪",
                    details=cdp.to_dict(),
                )
            resume = getattr(request, "resume_session", {}) or {}
            if (resume.get("resume") and resume.get("session_id")
                    and bool(self.config.get("chain_reuse_agent_sessions", True))):
                from ..gui.cdp import (switch_workbuddy_conversation,
                                       wait_for_workbuddy_conversation,
                                       wait_for_workbuddy_ready)
                session_id = str(resume["session_id"])
                # Verified on WorkBuddy 5.9.3: the chat deeplink actually
                # navigates and updates conversations.currentId; the renderer
                # setter alone only emits a bus event.
                opened = open_workbuddy_chat(
                    session_id=session_id, config=self.config)
                _log(log_path, "session_resume_deeplink", app=app,
                     **opened.to_dict())
                switched: dict[str, Any] = {"ok": False}
                if opened.ok:
                    switched = wait_for_workbuddy_conversation(
                        session_id, port=self.workbuddy_cdp_port,
                        timeout=float(self.config.get(
                            "workbuddy_session_switch_timeout_seconds") or 30),
                    )
                if not switched.get("ok"):
                    switched = switch_workbuddy_conversation(
                        session_id, port=self.workbuddy_cdp_port,
                        timeout=float(self.config.get(
                            "workbuddy_session_switch_timeout_seconds") or 30),
                    )
                    _log(log_path, "session_resume_switch", app=app,
                         session_id=session_id, **switched)
                    if switched.get("ok"):
                        ready = wait_for_workbuddy_ready(
                            self.workbuddy_cdp_port,
                            timeout=float(self.config.get(
                                "workbuddy_session_switch_timeout_seconds") or 30),
                        )
                        switched = {"ok": bool(ready.get("ok")), **ready}
                if switched.get("ok"):
                    time.sleep(1.0)
                verify_resume = verify_workbuddy_task(
                    workspace=workspace, model=request.model,
                    started_at=0, welcome_mode=welcome,
                    permission_mode=permission, session_id=session_id,
                    config=self.config,
                ) if switched.get("ok") else None
                if verify_resume is not None and verify_resume.ok:
                    profile_sync = sync_workbuddy_session_profile(
                        workspace=workspace, started_at=0,
                        session_id=session_id, model=request.model,
                        context_length=int(getattr(
                            request, "context_length", 0) or 0),
                        capability_level=str(getattr(
                            request, "capability_level", "") or ""),
                        config=self.config,
                    )
                    _log(log_path, "profile_sync", app=app,
                         **profile_sync.to_dict())
                    resume_target = ""
                    try:
                        from ..gui.cdp import _pick_page, _targets
                        _page = _pick_page(_targets(self.workbuddy_cdp_port))
                        resume_target = str((_page or {}).get("id") or "")
                    except Exception:
                        resume_target = ""
                    return {
                        "method": "workbuddy_resume_chat",
                        "model_key": model_key,
                        "cdp_target": resume_target,
                        "short_prompt": short_prompt,
                        "prepare": {
                            "ok": True,
                            "method": "cdp_conversation_switch",
                            "actual": switched,
                            "evidence": {"session_id": session_id,
                                         "resumed": True},
                        },
                        "prompt_file": str(prompt_copy),
                        "workspace": workspace,
                        "welcome_mode": welcome,
                        "permission_mode": permission,
                        "resumed_session_id": session_id,
                        "profile_sync": profile_sync.to_dict(),
                        "verify": lambda sid=session_id: verify_workbuddy_task(
                            workspace=workspace, model=request.model,
                            started_at=0, welcome_mode=welcome,
                            permission_mode=permission, session_id=sid,
                            config=self.config,
                        ),
                    }
                fallback_reason = (verify_resume.reason if verify_resume else
                                   switched.get("reason", ""))
                _log(log_path, "session_resume_fallback", app=app,
                     session_id=session_id, reason=fallback_reason)
                if not bool(self.config.get("allow_session_fallback", False)):
                    # Do not silently start a new session and call it a
                    # continuation.  The user (or an explicit policy) decides.
                    raise GuiAutomationError(
                        "SESSION_BINDING_UNCONFIRMED",
                        f"无法切回本运行的原 WorkBuddy 会话 {session_id}："
                        f"{fallback_reason or '会话归属无法确认'}",
                        details={"session_id": session_id,
                                 "reason": fallback_reason,
                                 "switched": switched,
                                 "verify": (verify_resume.to_dict()
                                            if verify_resume else {})},
                    )
            profile_ctx = int(getattr(request, "context_length", 0) or 0)
            profile_cap = str(getattr(request, "capability_level", "") or "")
            selection_sync: dict[str, Any] = {}
            if profile_ctx or profile_cap:
                from ..gui.cdp import sync_workbuddy_selection_preferences
                selection_sync = sync_workbuddy_selection_preferences(
                    port=self.workbuddy_cdp_port,
                    model_id=workbuddy_model_key(request.model),
                    context_length=profile_ctx,
                    capability_level=profile_cap,
                    user_id=workbuddy_user_id(self.config),
                    reload_if_changed=bool(
                        self.config.get("workbuddy_profile_reload", True)),
                )
                _log(log_path, "selection_preferences", app=app,
                     **selection_sync)
                if not selection_sync.get("ok"):
                    raise GuiAutomationError(
                        "PROFILE_SYNC_FAILED",
                        selection_sync.get("reason")
                        or "WorkBuddy 新任务上下文/能力偏好写入失败",
                        details=selection_sync,
                    )
                if selection_sync.get("reloaded"):
                    time.sleep(float(self.config.get(
                        "workbuddy_profile_reload_settle_seconds") or 5.0))
            if self.full_control:
                # WorkBuddy's task deeplink carries permissionMode=fullAccess
                # and workbuddy.db verifies it per session; the composer switch
                # belongs to the user's own chat, so we do not touch it.
                _log(log_path, "full_access", app=app, ok=True,
                     mode="fullAccess", method="deeplink")
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
            profile_sync = sync_workbuddy_session_profile(
                workspace=workspace,
                started_at=float(outcome.evidence.get("started_at") or started_epoch),
                model=request.model,
                context_length=int(getattr(request, "context_length", 0) or 0),
                capability_level=str(getattr(request, "capability_level", "") or ""),
                config=self.config,
            )
            _log(log_path, "profile_sync", app=app, **profile_sync.to_dict())
            if not profile_sync.ok:
                raise GuiAutomationError(
                    "PROFILE_SYNC_FAILED",
                    profile_sync.reason or "WorkBuddy 上下文/能力等级写入失败",
                    details=profile_sync.to_dict(),
                )
            cdp_target = ""
            try:
                from ..gui.cdp import _pick_page, _targets
                _page = _pick_page(_targets(self.workbuddy_cdp_port))
                cdp_target = str((_page or {}).get("id") or "")
            except Exception:
                cdp_target = ""
            return {
                "method": "workbuddy_deeplink", "model_key": model_key,
                "cdp_target": cdp_target,
                "short_prompt": short_prompt, "prepare": outcome.to_dict(),
                "prompt_file": str(prompt_copy), "workspace": workspace,
                "welcome_mode": welcome, "permission_mode": permission,
                "deeplink": str(outcome.evidence.get("uri")
                                or outcome.evidence.get("deeplink") or ""),
                "selection_preferences": selection_sync,
                "profile_sync": profile_sync.to_dict(),
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
                f"{plan_hint}\n"
                f"<!-- {MARKER_PREFIX}: {marker} -->\n"
            )
            try:
                reuse = verify_qoder_session(
                    workspace=workspace, model=request.model, started_at=0,
                    config=self.config, strict_daemon=False)
                active = qoder_active_session(self.config)
                running = qoder_app_running(self.config)
            except Exception as exc:
                # A missing/locked Qoder store must fall through to the
                # structured prepare error instead of crashing the turn.
                _log(log_path, "interface_probe_failed", app=app, error=str(exc)[:300])
                reuse, active, running = None, {}, False
            if reuse is not None and reuse.ok and running and \
                    str(active.get("session_id") or "") == str(
                        reuse.actual.get("session_id") or ""):
                # Qoder is already showing this exact run workspace/model (a
                # retry of the same attempt).  Reuse it without a restart.
                outcome = InterfaceOutcome(
                    True, "qoder_db_transcript",
                    {"workspace": workspace, "model": model_key},
                    {"session_id": reuse.actual.get("session_id", "")},
                    evidence={**reuse.evidence,
                              "reused_session_id": reuse.actual.get(
                                  "session_id", "")})
                _log(log_path, "interface_reuse", app=app,
                     session_id=reuse.actual.get("session_id", ""))
            else:
                outcome = prepare_qoder_session(
                    workspace=workspace, model=model_key, config=self.config,
                    permission_mode=("bypassPermissions" if self.full_control else ""),
                    allow_new_session=True,
                )
            _log(log_path, "interface_prepare", app=app, **outcome.to_dict())
            if outcome.ok:
                profile_sync = sync_qoder_model_profile(
                    model_key=qoder_model_key(request.model),
                    context_length=int(getattr(request, "context_length", 0) or 0),
                    capability_level=str(getattr(request, "capability_level", "") or ""),
                    config=self.config,
                )
                _log(log_path, "profile_sync", app=app, **profile_sync.to_dict())
                if not profile_sync.ok:
                    raise GuiAutomationError(
                        "PROFILE_SYNC_FAILED",
                        profile_sync.reason or "Qoder 上下文/能力等级写入失败",
                        details=profile_sync.to_dict(),
                    )
            else:
                profile_sync = None
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
                "profile_sync": (profile_sync.to_dict() if profile_sync else {}),
                "verify": lambda sid="": verify_qoder_session(
                    workspace=workspace, model=request.model,
                    started_at=started_epoch, config=self.config,
                    session_id=sid or str(outcome.evidence.get("session_id") or ""),
                ),
            }
        if app in ("trae_cn", "trae"):
            # Trae / Trae CN 1.107.1: plain SQLite applicationStorage +
            # CDP DOM.  The encrypted ai-agent database is never touched.
            dev_mode = str(self.config.get("trae_dev_mode") or "code")
            model_key = trae_interface.model_key(app, request.model, self.config)
            if not model_key:
                available = sorted({m["id"] for m in
                                    trae_interface.list_trae_models(app, self.config)})[:20]
                raise GuiAutomationError(
                    "MODEL_UNAVAILABLE",
                    f"Trae 真实模型清单中没有 {request.model}",
                    details={"requested": request.model, "available": available},
                )
            prompt_dir = ensure_dir(Path(workspace) / ".agentmesh")
            prompt_copy = prompt_dir / "interface-prompt.md"
            atomic_write_text(prompt_copy, prompt_with_marker)
            short_prompt = (
                f"请先读取并严格执行 `{prompt_copy}` 中的任务说明，"
                "完成后按其中的输出契约写结果。\n"
                f"{plan_hint}\n"
                f"<!-- {MARKER_PREFIX}: {marker} -->\n"
            )
            applied: list[Any] = []

            def _apply_state() -> None:
                applied.append(trae_interface.set_trae_workspace(
                    workspace=workspace, app=app, dev_mode=dev_mode, config=self.config))

            cdp = trae_interface.ensure_trae_cdp(
                app=app, port=self.trae_cdp_port, workspace=workspace,
                executable=str(self.config.get("executable") or ""),
                config=self.config, force_restart=False,
                allow_restart=self.allow_app_restart, before_launch=_apply_state,
            )
            _log(log_path, "cdp_ready", app=app, **cdp.to_dict())
            if not cdp.ok:
                raise GuiAutomationError(cdp.error_code or "GUI_SEND_FAILED",
                                         cdp.reason or "Trae CDP 未就绪",
                                         details=cdp.to_dict())
            if not applied and workspace:
                # Reuse path: the app was already running with CDP, so only
                # the folder-open CLI ran. Persist the mode/session keys the
                # verifier reads without ever restarting the editor.
                applied.append(trae_interface.set_trae_workspace(
                    workspace=workspace, app=app, dev_mode=dev_mode, config=self.config))
            workspace_outcome = applied[0] if applied else None
            if workspace_outcome is not None and not workspace_outcome.ok:
                raise GuiAutomationError(
                    workspace_outcome.error_code or "INTERFACE_WRITE_FAILED",
                    workspace_outcome.reason or "Trae 工作区写入失败",
                    details=workspace_outcome.to_dict(),
                )
            # Pin one renderer for model selection, prompt submission and
            # reply polling.  Trae can have several windows (folder CLI), and
            # a freshly launched window needs time to mount the workbench.
            # Prefer the page the workspace-open step proved belongs to this
            # folder; several plan worktrees are all named "workspace".
            trae_page_hint = str((cdp.evidence or {}).get("trae_page_id") or "")
            trae_page = trae_interface.wait_for_trae_chat(
                self.trae_cdp_port,
                timeout_seconds=float(
                    self.config.get("trae_chat_ready_timeout_seconds") or 30
                ),
                page_id=trae_page_hint,
            ) or trae_interface.pick_trae_page(self.trae_cdp_port,
                                               page_id=trae_page_hint)
            trae_page_id = str((trae_page or {}).get("id") or "")
            _log(log_path, "trae_page_pinned", page_id=trae_page_id,
                 title=str((trae_page or {}).get("title") or ""))
            if trae_interface.trae_login_required(
                    self.trae_cdp_port, page_id=trae_page_id):
                raise GuiAutomationError(
                    "APP_LOGIN_REQUIRED",
                    "Trae CN 当前页面显示登录墙，AI 模型不可用",
                    details={"page_id": trae_page_id},
                )
            # Model selection must happen while the renderer is alive: the SOLO
            # preference is server-synced and the composer is the authority.
            resolved = trae_interface.resolve_trae_model(
                request.model, app=app, config=self.config) or {}
            expected_display = str(resolved.get("display_name") or request.model)
            model_outcome = trae_interface.set_trae_model(
                model=request.model, app=app, config=self.config,
                cdp_port=self.trae_cdp_port, page_id=trae_page_id,
            )
            applied.append(model_outcome)
            _log(log_path, "interface_prepare", app=app,
                 applied=[o.to_dict() for o in applied])
            if not model_outcome.ok:
                raise GuiAutomationError(
                    model_outcome.error_code or "MODEL_SWITCH_UNSUPPORTED",
                    model_outcome.reason or "Trae 模型切换失败",
                    details=model_outcome.to_dict(),
                )
            visible = str(model_outcome.actual.get("visible_model") or "")
            _log(log_path, "trae_visible_model", visible=visible,
                 expected=expected_display)
            if not trae_interface.visible_model_matches(visible, expected_display):
                raise GuiAutomationError(
                    "MODEL_MISMATCH",
                    f"Trae 输入框显示模型为 {visible or '(空)'}，期望 {expected_display}",
                    details={"visible_model": visible, "expected": expected_display,
                             "model_outcome": model_outcome.to_dict()},
                )
            effort_sync = trae_interface.set_trae_reasoning_effort(
                model=request.model,
                effort=str(getattr(request, "capability_level", "") or ""),
                app=app, config=self.config,
            )
            _log(log_path, "profile_sync", app=app, **effort_sync.to_dict())
            if not effort_sync.ok:
                raise GuiAutomationError(
                    "PROFILE_SYNC_FAILED",
                    effort_sync.reason or "Trae 思考等级写入失败",
                    details=effort_sync.to_dict(),
                )
            return {
                "method": "trae_state_vscdb", "model_key": model_key,
                "short_prompt": short_prompt, "prompt_file": str(prompt_copy),
                "trae_page_id": trae_page_id,
                "profile_sync": effort_sync.to_dict(),
                "prepare": {
                    "workspace": workspace_outcome.to_dict() if workspace_outcome else {},
                    "model": model_outcome.to_dict() if model_outcome else {},
                    "cdp": cdp.to_dict(),
                },
                "verify": lambda _sid="": trae_interface.verify_trae_session(
                    workspace=workspace, model=request.model, dev_mode=dev_mode,
                    app=app, config=self.config, cdp_port=self.trae_cdp_port,
                    page_id=trae_page_id,
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
                                                config=self.config,
                                                allow_new_session=True)
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
                    permission_mode=(
                        str(self.config.get("workbuddy_permission_mode") or "fullAccess")
                        if self.full_control else ""
                    ),
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
            elif app in ("trae_cn", "trae"):
                key = trae_interface.model_key(app, model_key, self.config)
                if not key:
                    raise GuiAutomationError(
                        "MODEL_UNAVAILABLE",
                        f"Trae 真实模型清单中没有 {model_key}",
                    )
                cdp = trae_interface.ensure_trae_cdp(
                    app=app, port=self.trae_cdp_port,
                    workspace=str(self.config.get("model_probe_workspace") or ""),
                    config=self.config,
                )
                if not cdp.ok:
                    raise GuiAutomationError(cdp.error_code or "GUI_SEND_FAILED",
                                             cdp.reason or "Trae CDP 未就绪",
                                             details=cdp.to_dict())
                outcome = trae_interface.set_trae_model(
                    model=model_key, app=app, config=self.config,
                    cdp_port=self.trae_cdp_port,
                )
                evidence.update({"path": outcome.method, "actual": key,
                                 "visible": outcome.actual.get("visible_model", ""),
                                 "evidence": outcome.evidence})
                if not outcome.ok:
                    raise GuiAutomationError(
                        outcome.error_code or "MODEL_SWITCH_UNSUPPORTED",
                        outcome.reason or "Trae 模型切换失败",
                        details=outcome.to_dict(),
                    )
                verified = trae_interface.verify_trae_session(
                    workspace=str(self.config.get("model_probe_workspace") or ""),
                    model=model_key,
                    dev_mode=str(self.config.get("trae_dev_mode") or "code"),
                    app=app, config=self.config, cdp_port=self.trae_cdp_port,
                )
                if not verified.ok and verified.error_code == "GUI_MODEL_SWITCH_FAILED":
                    raise GuiAutomationError("MODEL_MISMATCH", verified.reason,
                                             details=verified.to_dict())
                evidence["ok"] = True
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
        result_path = Path(request.result_path)
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
                # A task must bring its own structured result.  The raw reply
                # is evidence/diagnostics; it is never turned into a synthetic
                # "completed" package with invented tests or empty risks.
                candidate = self._extract_json(reply.text)
                if candidate is None:
                    raise GuiAutomationError(
                        "GUI_RESULT_MISSING",
                        "任务回复没有写成果包，回复中也没有可解析的结果 JSON",
                        details={"result_path": str(request.result_path),
                                 "transcript": reply.transcript_path,
                                 "chars": len(reply.text)},
                    )
                try:
                    normalized, changed = ResultPackage.normalize(candidate)
                    ResultPackage.validate(
                        normalized, expected_task_id=request.task.task_id,
                        expected_agent_id=self.agent.agent_id,
                    )
                except Exception as exc:
                    raise GuiAutomationError(
                        "GUI_RESULT_INVALID",
                        f"回复中的成果候选不合法: {exc}",
                        details={"result_path": str(request.result_path),
                                 "candidate": candidate},
                    ) from exc
                result_path = Path(str(request.result_path) + ".reply-candidate.json")
                write_json(result_path, normalized)
                payload = normalized
                _log(log_path, "result_candidate_from_reply", path=str(result_path),
                     normalized=changed)
        elif purpose == "task":
            if not isinstance(payload, dict):
                raise GuiAutomationError(
                    "GUI_RESULT_INVALID",
                    "成果包必须是 JSON 对象",
                    details={"result_path": str(request.result_path),
                             "type": type(payload).__name__},
                )
            normalized, changed = ResultPackage.normalize(payload)
            try:
                ResultPackage.validate(
                    normalized, expected_task_id=request.task.task_id,
                    expected_agent_id=self.agent.agent_id,
                )
            except Exception as exc:
                # The raw, invalid package stays untouched for diagnosis.
                _log(log_path, "result_invalid", error=str(exc), normalized=changed,
                     path=str(request.result_path))
                raise GuiAutomationError(
                    "GUI_RESULT_INVALID",
                    f"成果包不合法: {exc}",
                    details={"result_path": str(request.result_path),
                             "normalized": changed},
                ) from exc
            payload = normalized
        interface_info = interface_info or {"method": "gui"}
        if payload is not None:
            evidence = payload.get("evidence")
            if isinstance(evidence, list):
                evidence.append(
                    f"interface: {interface_info.get('method')} "
                    f"workspace={interface_info.get('prepare', {}).get('expected', {}).get('workspace', '')}"
                )
        prepare = interface_info.get("prepare") if isinstance(interface_info, dict) else {}
        visible = interface_info.get("visible_model") or ""
        if not visible and isinstance(prepare, dict):
            actual = prepare.get("actual") or prepare.get("model") or {}
            if isinstance(actual, dict):
                visible = str(actual.get("visible_model") or actual.get("display_name") or "")
        model_evidence = {
            "requested_model": request.model,
            "selected_model_id": interface_info.get("model_key") or "",
            "visible_model_label": visible,
            "observed_response_model": reply.model or "",
            "observed_source": (reply.usage or {}).get("source", ""),
            "observed_known": bool(reply.model),
        }
        return ExecutionOutcome(
            status="completed", text=reply.text[-20000:], usage=usage,
            tool_calls=reply.tool_calls, result_payload=payload,
            result_path=str(result_path), logs=tail_text(log_path, max_chars=30000),
            session_id=reply.session_id, duration_ms=_duration_ms(started),
            extras={"gui_app": profile.app, "window_title": profile.window_title,
                    "model": reply.model, "model_key": model_key,
                    "model_evidence": model_evidence,
                    "transcript": reply.transcript_path, "screenshots": screenshots,
                    "events": reply.events, "interface": interface_info},
            submission_status=str((interface_info.get("submission") or {}).get("status") or ""),
            submission_receipt=dict(interface_info.get("submission") or {}),
        )

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
            total_tokens=total, cost=credits,
            currency=("credits" if credits > 0 else ""),
            cost_known=bool(credits > 0) if not estimated else False,
            tool_calls=len(reply.tool_calls),
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
