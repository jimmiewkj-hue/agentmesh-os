"""Transcript readers used as the GUI adapter's reply channel."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..util import now_iso
from .base import GuiAutomationError, GuiReply, GuiProfile, read_jsonl
from .trae_reader import TraeReader  # noqa: F401 (registered by create_reader)


def _result_file_ready(path: str) -> bool:
    if not path:
        return False
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    return isinstance(data, (dict, list))


def _candidate_files(root: Path, started_at: float, *, limit: int = 200) -> list[Path]:
    if not root.exists():
        return []
    files = [p for p in root.rglob("*.jsonl") if p.is_file()]
    files = [p for p in files if p.stat().st_mtime >= started_at - 2]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def _contains_marker(events: list[dict[str, Any]], marker: str) -> int:
    for index, event in enumerate(events):
        if event.get("type") not in ("user", "message"):
            continue
        if marker in str(event):
            return index
    return -1


def _text_from_blocks(blocks: Any, types: tuple[str, ...]) -> str:
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") in types:
            text = str(block.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


class QoderCnReader:
    """Qoder CN: ~/.qoder-cn/projects/**/*.jsonl (main + subagents)."""

    def __init__(self, profile: GuiProfile, config: dict[str, Any] | None = None):
        self.profile = profile
        self.config = dict(config or {})
        root = self.config.get("data_path") or profile.data_root
        self.root = Path(root)
        self.send_timeout = float(self.config.get("send_timeout_seconds", 90))
        self.cancel_event = None

    def wait_reply(self, marker: str, *, started_at: float, timeout: float,
                   stall_seconds: float, expected_model: str = "") -> GuiReply:
        deadline = time.monotonic() + timeout
        send_deadline = time.monotonic() + min(self.send_timeout, timeout)
        last_size: dict[Path, int] = {}
        last_growth: dict[Path, float] = {}
        found_any = False
        while time.monotonic() < deadline:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise GuiAutomationError("GUI_CANCELED", "用户停止运行")
            for path in _candidate_files(self.root, started_at):
                events = read_jsonl(path)
                index = _contains_marker(events, marker)
                if index < 0:
                    continue
                found_any = True
                texts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                runtime_model = ""
                assistant_model = ""
                usage: dict[str, Any] = {}
                completed = False
                for event in events[index:]:
                    etype = event.get("type")
                    if etype == "runtime-config" and event.get("model"):
                        # Runtime config can be written after the assistant
                        # message and describes the next turn, so it is only
                        # a fallback behind the model that actually answered.
                        runtime_model = str(event["model"])
                    if etype != "assistant":
                        continue
                    message = event.get("message") or {}
                    if not isinstance(message, dict):
                        continue
                    assistant_model = str(message.get("model") or assistant_model)
                    blocks = message.get("content")
                    texts.append(_text_from_blocks(blocks, ("text",)))
                    if isinstance(blocks, list):
                        for block in blocks:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                tool_calls.append({
                                    "name": str(block.get("name") or ""),
                                    "input": block.get("input"),
                                })
                    raw_usage = message.get("usage") or {}
                    if isinstance(raw_usage, dict) and raw_usage:
                        usage = {
                            "input_tokens": int(raw_usage.get("input_tokens", 0) or 0),
                            "output_tokens": int(raw_usage.get("output_tokens", 0) or 0),
                            "cached_tokens": int(raw_usage.get(
                                "cache_read_input_tokens", raw_usage.get("cached_tokens", 0)) or 0),
                            "reasoning_tokens": int(raw_usage.get("reasoning_tokens", 0) or 0),
                            "credits": float(raw_usage.get("credits", 0) or 0),
                        }
                    if message.get("stop_reason") == "end_turn":
                        completed = True
                if completed and any(texts):
                    model = assistant_model or runtime_model
                    text = "\n".join(t for t in texts if t).strip()
                    if expected_model and model and model != expected_model:
                        raise GuiAutomationError(
                            "GUI_MODEL_SWITCH_FAILED",
                            f"Qoder CN 实际模型 {model} 与请求 {expected_model} 不一致",
                            details={"expected": expected_model, "actual": model,
                                     "transcript": str(path)},
                        )
                    return GuiReply(
                        session_id=self._session_id(events, path),
                        text=text, model=model, transcript_path=str(path),
                        tool_calls=tool_calls, usage=usage, events=len(events),
                        finished_at=now_iso(),
                    )
                now = time.monotonic()
                size = path.stat().st_size
                if last_size.get(path) != size:
                    last_size[path] = size
                    last_growth[path] = now
                if now - last_growth.get(path, now) > stall_seconds:
                    raise GuiAutomationError(
                        "GUI_STALLED",
                        f"Qoder CN 会话 {path.stem} 在 {stall_seconds:.0f}s 内无新增事件",
                        details={"transcript": str(path), "marker": marker},
                    )
            if not found_any and time.monotonic() > send_deadline:
                raise GuiAutomationError(
                    "GUI_SEND_FAILED",
                    f"任务发送后 {self.send_timeout:.0f}s 内未在 Qoder CN 会话中看到标记",
                    details={"marker": marker, "root": str(self.root)},
                )
            time.sleep(float(self.config.get("poll_seconds", 2.0)))
        raise GuiAutomationError(
            "GUI_TIMEOUT", f"Qoder CN 在 {timeout:.0f}s 内未完成回复",
            details={"marker": marker},
        )

    @staticmethod
    def _session_id(events: list[dict[str, Any]], path: Path) -> str:
        for event in events:
            if event.get("sessionId") or event.get("session_id"):
                return str(event.get("sessionId") or event.get("session_id"))
        return path.stem


class WorkBuddyReader:
    """WorkBuddy: ~/.workbuddy/projects/**/*.jsonl + workbuddy.db sessions."""

    def __init__(self, profile: GuiProfile, config: dict[str, Any] | None = None):
        self.profile = profile
        self.config = dict(config or {})
        root = self.config.get("data_path") or profile.data_root
        self.root = Path(root)
        self.db_path = Path(self.config.get("session_db") or
                            (Path.home() / ".workbuddy" / "workbuddy.db"))
        self.send_timeout = float(self.config.get("send_timeout_seconds", 90))
        self.quiet_seconds = float(self.config.get("quiet_seconds", 10))
        # A result file can be written before the agent finishes its final
        # self-check (observed on 2026-11-05: 43s of continued work after
        # result.json).  Require a settled transcript before declaring done.
        self.result_settle_seconds = float(
            self.config.get("workbuddy_result_settle_seconds", 20))
        self.cdp_port = int(self.config.get("workbuddy_cdp_port") or 9223)
        self.completion_path = ""
        self.cancel_event = None

    def wait_reply(self, marker: str, *, started_at: float, timeout: float,
                   stall_seconds: float, expected_model: str = "") -> GuiReply:
        deadline = time.monotonic() + timeout
        send_deadline = time.monotonic() + min(self.send_timeout, timeout)
        last_size: dict[Path, int] = {}
        last_activity: dict[Path, float] = {}
        result_ready_since: dict[Path, float] = {}
        found_any = False
        while time.monotonic() < deadline:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise GuiAutomationError("GUI_CANCELED", "用户停止运行")
            for path in _candidate_files(self.root, started_at):
                events = read_jsonl(path)
                index = _contains_marker(events, marker)
                if index < 0:
                    continue
                found_any = True
                texts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                model = ""
                usage = {}
                session_id = self._session_id(events, path)
                meaningful: list[dict[str, Any]] = []
                for event in events[index:]:
                    etype = event.get("type")
                    if etype in ("message", "reasoning", "function_call", "function_call_result"):
                        meaningful.append(event)
                    if etype == "function_call":
                        try:
                            arguments = event.get("arguments")
                            if isinstance(arguments, str):
                                import json
                                arguments = json.loads(arguments)
                        except Exception:
                            arguments = event.get("arguments")
                        tool_calls.append({"name": str(event.get("name") or ""),
                                           "input": arguments})
                    if etype != "message":
                        continue
                    provider = event.get("providerData") or {}
                    if isinstance(provider, dict):
                        model = str(provider.get("requestModelName") or provider.get("model") or model)
                        raw = provider.get("usage") or {}
                        if isinstance(raw, dict) and raw:
                            usage = {
                                "input_tokens": int(raw.get("inputTokens", 0) or 0),
                                "output_tokens": int(raw.get("outputTokens", 0) or 0),
                                "cached_tokens": sum(
                                    int(d.get("cached_tokens", 0) or 0)
                                    for d in (raw.get("inputTokensDetails") or [])
                                    if isinstance(d, dict)
                                ),
                                "reasoning_tokens": sum(
                                    int(d.get("reasoning_tokens", 0) or 0)
                                    for d in (raw.get("outputTokensDetails") or [])
                                    if isinstance(d, dict)
                                ),
                                "total_tokens": int(raw.get("totalTokens", 0) or 0),
                            }
                        raw_usage = provider.get("rawUsage") or {}
                        if isinstance(raw_usage, dict) and raw_usage.get("credit") is not None:
                            usage["credits"] = float(raw_usage.get("credit") or 0)
                    if event.get("role") == "assistant":
                        texts.append(_text_from_blocks(event.get("content"),
                                                       ("output_text", "text")))
                db_status = self._db_status(session_id)
                last = meaningful[-1] if meaningful else {}
                last_error = ""
                if last.get("type") == "message" and last.get("role") == "assistant":
                    provider_data = last.get("providerData") or {}
                    error = provider_data.get("error") if isinstance(provider_data, dict) else None
                    if isinstance(error, dict):
                        last_error = str(error.get("message") or "")
                    elif error:
                        last_error = str(error)
                    last_status = str(last.get("status") or "")
                    if last_status in ("incomplete", "error") and last_error:
                        code = "GUI_CANCELED" if "Interrupted by user" in last_error \
                            else "MODEL_STREAM_ERROR"
                        raise GuiAutomationError(
                            code,
                            f"WorkBuddy 模型流中断: {last_error}",
                            details={"session": session_id, "transcript": str(path),
                                     "status": last_status, "provider_error": last_error},
                        )
                if db_status in ("failed", "error", "cancelled", "canceled"):
                    raise GuiAutomationError(
                        "GUI_DRIVER_ERROR",
                        "WorkBuddy 会话状态为 "
                        f"{db_status}{(': ' + last_error) if last_error else ''}",
                        details={"session": session_id, "transcript": str(path),
                                 "provider_error": last_error},
                    )
                now = time.monotonic()
                size = path.stat().st_size
                if last_size.get(path) != size:
                    last_size[path] = size
                    last_activity[path] = now
                    # The agent rewrote the transcript: any earlier result
                    # file is stale and the settle timer restarts.
                    result_ready_since.pop(path, None)
                if _result_file_ready(self.completion_path):
                    result_ready_since.setdefault(path, now)
                    stable_for = now - last_activity.get(path, now)
                    ready_for = now - result_ready_since.get(path, now)
                    # A transient ``completed`` DB status between agent steps
                    # is not a final signal.  When the session is still
                    # working, keep waiting for the application's own final
                    # status even though result.json already exists.
                    final_by_db = db_status == "completed"
                    final_by_quiet = (
                        db_status not in ("working", "running", "pending")
                        and stable_for >= self.result_settle_seconds
                        and ready_for >= self.result_settle_seconds
                    )
                    if final_by_db or final_by_quiet:
                        text = "\n".join(t for t in texts if t).strip() or \
                            "结果文件已写入，详见 result.json"
                        actual = self._db_model(session_id) or model
                        if expected_model and expected_model.lower() != actual.lower():
                            raise GuiAutomationError(
                                "GUI_MODEL_SWITCH_FAILED",
                                f"WorkBuddy 实际模型 {actual or model} 与请求 {expected_model} 不一致",
                                details={"expected": expected_model, "actual": actual,
                                         "transcript_model": model,
                                         "transcript": str(path)},
                            )
                        return GuiReply(
                            session_id=session_id, text=text[-60000:], model=actual or model,
                            transcript_path=str(path), tool_calls=tool_calls,
                            usage={**usage, "result_path": self.completion_path},
                            events=len(events), finished_at=now_iso(),
                        )
                    continue
                last = meaningful[-1] if meaningful else {}
                last_is_final = (
                    last.get("type") == "message"
                    and last.get("role") == "assistant"
                    and last.get("status") == "completed"
                )
                if (db_status == "completed" or (
                        last_is_final and (
                            not self.completion_path
                            or db_status not in ("working", "running", "pending")
                        )
                )) and any(texts):
                    # A completed assistant message may be an intermediate step.
                    # With a result-file contract, never finalize while the app
                    # says the session is still working (observed 43s of final
                    # self-check after result.json).  Other WorkBuddy turns
                    # without a result path rely on the quiet period.
                    if db_status != "completed":
                        if now - last_activity.get(path, now) < self.quiet_seconds:
                            continue
                    text = "\n".join(t for t in texts if t).strip()
                    actual = self._db_model(session_id) or model
                    if expected_model:
                        if expected_model.lower() != actual.lower():
                            raise GuiAutomationError(
                                "GUI_MODEL_SWITCH_FAILED",
                                f"WorkBuddy 实际模型 {actual or model} 与请求 {expected_model} 不一致",
                                details={"expected": expected_model, "actual": actual,
                                         "transcript_model": model,
                                         "transcript": str(path)},
                            )
                    return GuiReply(
                        session_id=session_id, text=text, model=actual or model,
                        transcript_path=str(path), tool_calls=tool_calls,
                        usage=usage, events=len(events), finished_at=now_iso(),
                    )
                now = time.monotonic()
                size = path.stat().st_size
                if last_size.get(path) != size:
                    last_size[path] = size
                    last_activity[path] = now
                if now - last_activity.get(path, now) > stall_seconds:
                    # The model may stream for 10-15 minutes without a
                    # transcript event while the app still shows
                    # 深度思考中 / a stop button.  Neither "working" nor a live
                    # UI is a stall; wait for the app's own error/complete.
                    if db_status in ("working", "running", "pending") or self.ui_busy():
                        last_activity[path] = now
                        continue
                    raise GuiAutomationError(
                        "GUI_STALLED",
                        f"WorkBuddy 会话 {path.stem} 在 {stall_seconds:.0f}s 内无新增事件，"
                        "且应用未显示思考中/停止按钮",
                        details={"transcript": str(path), "marker": marker,
                                 "session_status": db_status},
                    )
            if not found_any and time.monotonic() > send_deadline:
                raise GuiAutomationError(
                    "GUI_SEND_FAILED",
                    f"任务发送后 {self.send_timeout:.0f}s 内未在 WorkBuddy 会话中看到标记",
                    details={"marker": marker, "root": str(self.root)},
                )
            time.sleep(float(self.config.get("poll_seconds", 2.0)))
        raise GuiAutomationError(
            "GUI_TIMEOUT", f"WorkBuddy 在 {timeout:.0f}s 内未完成回复",
            details={"marker": marker},
        )

    def ui_busy(self) -> bool:
        """True when WorkBuddy's own UI still shows an active turn.

        The model may stream for many minutes without writing transcript
        events; a transcript-only stall timer then fails a healthy task while
        the app still displays 深度思考中 / a stop button.  Probe the app's own
        renderer before declaring a stall.
        """
        try:
            from .cdp import WebSocketTransport, _evaluate, _pick_page, _targets
            page = _pick_page(_targets(self.cdp_port))
            if not page:
                return False
            transport = WebSocketTransport(
                page["webSocketDebuggerUrl"],
                origin=f"http://127.0.0.1:{int(self.cdp_port)}", timeout=5)
            try:
                value = _evaluate(transport, """
(() => {
  const text = (document.body && document.body.innerText) || '';
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  let stop = false;
  for (const el of document.querySelectorAll('button,[role="button"],div')) {
    if (!visible(el)) continue;
    const label = [el.getAttribute('aria-label'), el.getAttribute('title'),
                   el.className, el.textContent].filter(Boolean).join(' ');
    if (/(^|\\s|_)(stop|停止)(\\s|$|_)/i.test(label)) { stop = true; break; }
  }
  const thinking = /(深度思考中|生成回复中|正在思考|思考中|正在生成|thinking|generating)/i.test(text);
  return {busy: stop || thinking, stop, thinking};
})()
""", 5) or {}
                return bool(value.get("busy"))
            finally:
                transport.close()
        except Exception:
            return False

    def _db_model(self, session_id: str) -> str:
        return str(self._db_row(session_id).get("model") or "")

    def _db_status(self, session_id: str) -> str:
        return str(self._db_row(session_id).get("status") or "")

    def active_sessions(self, *, max_idle_seconds: float = 900.0) -> list[dict[str, Any]]:
        """Sessions the WorkBuddy app still considers active.

        WorkBuddy can host several task conversations at once.  Starting a new
        task while another is still working is what made T002 overlap T001 on
        2026-11-05.  The adapter serializes on this list before sending.
        """
        if not self.db_path.exists():
            return []
        cutoff_ms = (time.time() - max(30.0, float(max_idle_seconds))) * 1000
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=3)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT id, cwd, title, status, model, created_at, updated_at, "
                    "last_activity_at FROM sessions "
                    "WHERE status IN ('working','pending','running') "
                    "AND MAX(COALESCE(last_activity_at,0), COALESCE(updated_at,0), "
                    "        COALESCE(created_at,0)) >= ? "
                    "ORDER BY MAX(COALESCE(last_activity_at,0), COALESCE(updated_at,0), "
                    "            COALESCE(created_at,0)) DESC",
                    (cutoff_ms,),
                ).fetchall()
            finally:
                conn.close()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            return []

    def _db_row(self, session_id: str) -> dict[str, Any]:
        if not session_id or not self.db_path.exists():
            return {}
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=2)
            try:
                cursor = conn.execute(
                    "SELECT model, status FROM sessions WHERE id = ?", (session_id,)
                )
                row = cursor.fetchone()
            finally:
                conn.close()
            return {"model": row[0], "status": row[1]} if row else {}
        except sqlite3.Error:
            return {}

    @staticmethod
    def _session_id(events: list[dict[str, Any]], path: Path) -> str:
        for event in events:
            if event.get("sessionId") or event.get("session_id"):
                return str(event.get("sessionId") or event.get("session_id"))
        return path.stem


def create_reader(profile: GuiProfile, config: dict[str, Any] | None = None):
    if profile.transcript_reader == "qoder_cn":
        return QoderCnReader(profile, config)
    if profile.transcript_reader == "workbuddy":
        return WorkBuddyReader(profile, config)
    if profile.transcript_reader in ("trae_cn", "trae"):
        from .trae_reader import create_trae_reader
        return create_trae_reader(profile, config)
    raise GuiAutomationError(
        "GUI_APP_NOT_CONFIGURED", f"未知 transcript_reader: {profile.transcript_reader}"
    )


__all__ = ["QoderCnReader", "WorkBuddyReader", "TraeReader", "create_reader"]
