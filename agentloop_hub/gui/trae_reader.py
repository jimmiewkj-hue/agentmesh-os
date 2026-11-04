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
        self._text_reader = text_reader or (
            lambda: trae.trae_conversation_text(port=self.port))

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
            time.sleep(self.poll_interval)
            try:
                state = self._state_reader()
                text_result = self._text_reader() or {}
            except Exception as exc:
                raise GuiAutomationError("GUI_DRIVER_ERROR",
                                         f"Trae 轮询失败: {exc}") from exc
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
            if marker_seen and saw_reply and stable and (finished or grew):
                recent = state.get(
                    state.prefixed("AI.agent.model.recent_user_selection_by_agent_label"), {})
                selection = recent.get(trae.AGENT_LABEL, {}) if isinstance(recent, dict) else {}
                model = str(selection.get("modelId", "")) if isinstance(selection, dict) else ""
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

    @staticmethod
    def _after_marker(text: str, marker: str) -> str:
        index = text.find(marker)
        if index < 0:
            return ""
        return text[index + len(marker):].strip()


def create_trae_reader(profile: GuiProfile, config: dict[str, Any] | None = None) -> TraeReader:
    return TraeReader(profile, config)


__all__ = ["TraeReader", "create_trae_reader"]
