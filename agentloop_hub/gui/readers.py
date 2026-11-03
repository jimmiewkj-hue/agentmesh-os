"""Transcript readers used as the GUI adapter's reply channel."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from ..util import now_iso
from .base import GuiAutomationError, GuiReply, GuiProfile, read_jsonl


def _candidate_files(root: Path, started_at: float, *, limit: int = 20) -> list[Path]:
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

    def wait_reply(self, marker: str, *, started_at: float, timeout: float,
                   stall_seconds: float, expected_model: str = "") -> GuiReply:
        deadline = time.monotonic() + timeout
        send_deadline = time.monotonic() + min(self.send_timeout, timeout)
        last_size = -1
        last_growth = time.monotonic()
        found_any = False
        while time.monotonic() < deadline:
            for path in _candidate_files(self.root, started_at):
                events = read_jsonl(path)
                index = _contains_marker(events, marker)
                if index < 0:
                    continue
                found_any = True
                texts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                model = ""
                usage: dict[str, Any] = {}
                completed = False
                for event in events[index:]:
                    etype = event.get("type")
                    if etype == "runtime-config" and event.get("model"):
                        model = str(event["model"])
                    if etype != "assistant":
                        continue
                    message = event.get("message") or {}
                    if not isinstance(message, dict):
                        continue
                    model = str(message.get("model") or model)
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
                size = path.stat().st_size
                if size != last_size:
                    last_size = size
                    last_growth = time.monotonic()
                if time.monotonic() - last_growth > stall_seconds:
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

    def wait_reply(self, marker: str, *, started_at: float, timeout: float,
                   stall_seconds: float, expected_model: str = "") -> GuiReply:
        deadline = time.monotonic() + timeout
        send_deadline = time.monotonic() + min(self.send_timeout, timeout)
        last_size = -1
        last_growth = time.monotonic()
        found_any = False
        while time.monotonic() < deadline:
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
                completed = False
                for event in events[index:]:
                    etype = event.get("type")
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
                        if event.get("status") == "completed":
                            completed = True
                if completed and any(texts):
                    text = "\n".join(t for t in texts if t).strip()
                    session_id = self._session_id(events, path)
                    db_model = self._db_model(session_id)
                    actual = db_model or model
                    if expected_model:
                        candidates = {expected_model.lower(), model.lower(),
                                      actual.lower()}
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
                size = path.stat().st_size
                if size != last_size:
                    last_size = size
                    last_growth = time.monotonic()
                if time.monotonic() - last_growth > stall_seconds:
                    raise GuiAutomationError(
                        "GUI_STALLED",
                        f"WorkBuddy 会话 {path.stem} 在 {stall_seconds:.0f}s 内无新增事件",
                        details={"transcript": str(path), "marker": marker},
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

    def _db_model(self, session_id: str) -> str:
        if not session_id or not self.db_path.exists():
            return ""
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=2)
            try:
                row = conn.execute(
                    "SELECT model FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
            finally:
                conn.close()
            return str(row[0] or "") if row else ""
        except sqlite3.Error:
            return ""

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
    raise GuiAutomationError(
        "GUI_APP_NOT_CONFIGURED", f"未知 transcript_reader: {profile.transcript_reader}"
    )


__all__ = ["QoderCnReader", "WorkBuddyReader", "create_reader"]
