"""Trae / Trae CN reply reader using the application's own visible DOM + state.

The full Trae chat transcript lives in an encrypted database
(``ModularData/ai-agent/database.db``); we deliberately do not read or crack it.
Two verified, zero-input signals are used instead:

* ``User/globalStorage/state.vscdb`` -> ``solo-lite-mode-state-map-<uid>``
  session carries ``messageIds`` and ``assistant_task_status`` ("finished").
* CDP ``Runtime.evaluate`` -> ``document.body.innerText`` gives the visible
  conversation, including the marker we inserted and the assistant reply.

Completion is declared only when the reply text is non-empty, has stopped
changing for ``stable_seconds`` and the session reports ``finished`` (or the
message list has grown).  No mouse, keyboard or clipboard event is synthesised.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from ..control import trae_interface as trae
from ..util import now_iso
from .base import GuiAutomationError, GuiProfile, GuiReply


def _active_session(state: trae.TraeState) -> tuple[str, dict[str, Any]]:
    modes = state.get(f"solo-lite-mode-state-map-{state.uid}", {})
    if isinstance(modes, dict):
        for mode in trae.DEV_MODES:
            entry = modes.get(mode)
            session = entry.get("session") if isinstance(entry, dict) else None
            if isinstance(session, dict) and session.get("sessionId"):
                return mode, session
    return "", {}


def _json_file_ready(path: str) -> bool:
    """A GUI plan/task is done when its result file is complete valid JSON."""
    if not path:
        return False
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    return isinstance(data, (dict, list))


class TraeReader:
    def __init__(self, profile: GuiProfile, config: dict[str, Any] | None = None, *,
                 state_reader: Callable[[], trae.TraeState] | None = None,
                 text_reader: Callable[[], dict[str, Any]] | None = None):
        self.profile = profile
        self.config = dict(config or {})
        self.app = str(self.config.get("gui_app") or profile.app or "trae_cn")
        self.port = int(self.config.get("trae_cdp_port") or trae.CDP_DEFAULT_PORT)
        self.poll_interval = float(self.config.get("trae_poll_seconds") or 1.5)
        self.stable_seconds = float(self.config.get("trae_stable_seconds") or 4.0)
        self._state_reader = state_reader or (
            lambda: trae.read_state(self.app, self.config))
        self.page_id = ""
        self._text_reader = text_reader or (
            lambda: trae.trae_conversation_text(port=self.port,
                                                page_id=self.page_id))
        self.completion_path = ""
        self.cancel_event = None

    def wait_reply(self, marker: str, *, started_at: float, timeout: float,
                   stall_seconds: float, expected_model: str = "") -> GuiReply:
        deadline = time.monotonic() + float(timeout)
        try:
            initial = self._state_reader()
            _, session = _active_session(initial)
            baseline_messages = len(session.get("messageIds") or [])
            baseline_text = str((self._text_reader() or {}).get("text") or "")
        except Exception as exc:
            raise GuiAutomationError("GUI_DRIVER_ERROR",
                                     f"Trae 初始状态读取失败: {exc}") from exc

        last_text = baseline_text
        last_change = time.monotonic()
        saw_reply = False
        while time.monotonic() < deadline:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise GuiAutomationError("GUI_CANCELED", "用户停止运行")
            time.sleep(self.poll_interval)
            try:
                state = self._state_reader()
                text_result = self._text_reader() or {}
            except Exception as exc:
                raise GuiAutomationError("GUI_DRIVER_ERROR",
                                         f"Trae 轮询失败: {exc}") from exc
            if text_result.get("page_id"):
                self.page_id = str(text_result["page_id"])
            text = str(text_result.get("text") or "")
            _, session = _active_session(state)
            messages = len(session.get("messageIds") or [])
            finished = str(session.get("assistant_task_status") or "").lower() == "finished"
            marker_seen = marker in text or marker in baseline_text
            reply = self._after_marker(text, marker)
            if reply and reply.strip():
                saw_reply = True
            if text != last_text:
                last_text = text
                last_change = time.monotonic()
            stable = time.monotonic() - last_change >= self.stable_seconds
            grew = messages > baseline_messages
            recent = state.get(
                state.prefixed("AI.agent.model.recent_user_selection_by_agent_label"), {})
            selection = recent.get(trae.AGENT_LABEL, {}) if isinstance(recent, dict) else {}
            model = str(selection.get("modelId", "")) if isinstance(selection, dict) else ""
            if _json_file_ready(self.completion_path) and marker_seen \
                    and saw_reply and finished and grew:
                # The result file is evidence from this turn, not a terminal
                # signal by itself.  The application must also report the
                # assistant turn finished and the session must have grown
                # since the request was sent.
                return GuiReply(
                    session_id=str(session.get("sessionId") or ""),
                    text=(reply or text)[-60000:],
                    model=model,
                    transcript_path=str(state.db),
                    events=max(0, messages - baseline_messages),
                    usage={"source": "cdp-dom+state.vscdb+result-file",
                           "message_ids": messages,
                           "result_path": self.completion_path},
                    finished_at=now_iso(),
                )
            # ``grew`` alone is not a completion signal: Trae appends a user
            # message before the assistant starts, and a plan/task may expose a
            # progress line while still working. Only the application's own
            # assistant_task_status=finished plus a new message pair is final.
            if marker_seen and saw_reply and stable and finished and grew:
                return GuiReply(
                    session_id=str(session.get("sessionId") or ""),
                    text=reply[-60000:],
                    model=model or expected_model,
                    transcript_path=str(state.db),
                    events=max(0, messages - baseline_messages),
                    usage={"source": "cdp-dom+state.vscdb", "message_ids": messages},
                    finished_at=now_iso(),
                )
            if stall_seconds and time.monotonic() - last_change > stall_seconds \
                    and not saw_reply:
                raise GuiAutomationError(
                    "GUI_STALLED",
                    "Trae 会话长时间无新增可见回复事件",
                    details={"marker": marker, "messages": messages},
                )
        raise GuiAutomationError(
            "GUI_TIMEOUT",
            f"等待 Trae 回复超时（{timeout}s）",
            details={"marker": marker, "saw_reply": saw_reply,
                     "baseline_messages": baseline_messages},
        )

    def has_marker(self, marker: str, *, started_at: float = 0.0) -> dict[str, Any]:
        """Trae's visible body also contains the composer, so a marker there
        does not prove the message was sent.  Report unsupported rather than
        inventing an acceptance receipt."""
        return {"ok": False, "reason": "NO_TRANSCRIPT_RECEIPT"}

    @staticmethod
    def _after_marker(text: str, marker: str) -> str:
        index = text.find(marker)
        if index < 0:
            return ""
        return text[index + len(marker):].strip()


def create_trae_reader(profile: GuiProfile, config: dict[str, Any] | None = None) -> TraeReader:
    return TraeReader(profile, config)


__all__ = ["TraeReader", "create_trae_reader"]
