"""Shared session parsing helpers (deterministic, no LLM)."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..util import iso_from_epoch_ms, sha1_hex

SYSTEM_REMINDER_RE = re.compile(r"<system-reminder[^>]*>.*?</system-reminder>", re.S | re.I)
INJECTED_BLOCK_RE = re.compile(
    r"<(identity_context|user_info|workspace_context|additional_instructions)[^>]*>.*?</\1>",
    re.S | re.I,
)


@dataclass
class ParsedMessage:
    role: str
    text: str
    timestamp: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role, "text": self.text, "timestamp": self.timestamp,
            "tool_calls": self.tool_calls, "reasoning": self.reasoning,
        }


@dataclass
class ParsedSession:
    session_id: str
    source_path: str
    title: str = ""
    cwd: str = ""
    started_at: str = ""
    ended_at: str = ""
    messages: list[ParsedMessage] = field(default_factory=list)
    token_usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    fingerprint: str = ""

    def add_text(self, role: str, text: str, timestamp: str = "",
                 tool_calls: list[dict[str, Any]] | None = None, reasoning: str = "") -> None:
        text = sanitize_text(text)
        if not text and not tool_calls:
            return
        self.messages.append(ParsedMessage(role, text, timestamp, tool_calls or [], reasoning))


def sanitize_text(text: Any) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            import json
            text = json.dumps(text, ensure_ascii=False)
        except Exception:
            text = str(text)
    text = SYSTEM_REMINDER_RE.sub("", text)
    text = INJECTED_BLOCK_RE.sub("", text)
    # WorkBuddy/Qoder sometimes prepend a metadata header; strip only known tags.
    text = re.sub(r"<user_info>.*?</user_info>", "", text, flags=re.S | re.I)
    return text.strip()


def text_from_blocks(content: Any, *, include_types: Iterable[str] = ("text", "input_text", "output_text"),
                     reasoning_types: Iterable[str] = ("thinking", "reasoning", "reasoning_text")) -> tuple[str, str]:
    if isinstance(content, str):
        return sanitize_text(content), ""
    if not isinstance(content, list):
        return "", ""
    texts: list[str] = []
    reasoning: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")
        if btype in include_types:
            value = block.get("text") or block.get("content") or ""
            if isinstance(value, str) and value.strip():
                texts.append(sanitize_text(value))
        elif btype in reasoning_types:
            value = block.get("thinking") or block.get("text") or block.get("content") or ""
            if isinstance(value, str) and value.strip():
                reasoning.append(str(value).strip())
    return "\n".join(t for t in texts if t), "\n".join(reasoning)


def tool_calls_from_blocks(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    calls = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            calls.append({
                "id": str(block.get("id") or ""),
                "name": str(block.get("name") or ""),
                "input": block.get("input") or {},
            })
    return calls


def tool_results_from_blocks(content: Any) -> list[str]:
    if not isinstance(content, list):
        return []
    out = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            value = block.get("content")
            if isinstance(value, str):
                out.append(sanitize_text(value))
            elif isinstance(value, list):
                text, _ = text_from_blocks(value)
                out.append(text)
    return [t for t in out if t]


def parse_timestamp(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e15:
            v /= 1000.0
        if v > 1e12:
            return iso_from_epoch_ms(int(v)) or ""
        return iso_from_epoch_ms(int(v * 1000)) or ""
    text = str(value).strip()
    if text.isdigit():
        return parse_timestamp(int(text))
    if text.endswith("Z") or "+" in text or "T" in text:
        return text
    return text


def normalize_cwd(cwd: str) -> str:
    return str(cwd or "").replace("\\", "/").rstrip("/").lower()


def project_matches(cwd: str, project_root: str) -> bool:
    if not cwd:
        return False
    a = normalize_cwd(cwd)
    b = normalize_cwd(project_root)
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def file_fingerprint(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return ""
    return sha1_hex(f"{path.name}:{st.st_size}:{st.st_mtime_ns}")


def summarize(session: ParsedSession) -> dict[str, Any]:
    """Deterministic conversation summary.

    Only extracts what is literally present: user requests, the last assistant
    replies, tool names, counts and token totals. It deliberately does not infer
    decisions or facts; that is the main node's job when it consumes this archive.
    """
    user_messages = [m for m in session.messages if m.role == "user" and m.text]
    assistant_messages = [m for m in session.messages if m.role == "assistant" and m.text]
    tool_names: list[str] = []
    for m in session.messages:
        for call in m.tool_calls:
            name = str(call.get("name") or "")
            if name and name not in tool_names:
                tool_names.append(name)
    return {
        "method": "deterministic",
        "title": session.title,
        "model": session.model,
        "message_count": len(session.messages),
        "user_request_count": len(user_messages),
        "assistant_reply_count": len(assistant_messages),
        "user_requests": [m.text[:400] for m in user_messages[:10]],
        "recent_user_requests": [m.text[:400] for m in user_messages[-5:]],
        "last_assistant_reply": (assistant_messages[-1].text[:800] if assistant_messages else ""),
        "tool_names": tool_names[:50],
        "token_usage": session.token_usage,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
    }


def token_totals(*, input_tokens: int = 0, output_tokens: int = 0,
                 cached_tokens: int = 0, reasoning_tokens: int = 0) -> dict[str, int]:
    return {
        "input": int(input_tokens or 0),
        "output": int(output_tokens or 0),
        "cached": int(cached_tokens or 0),
        "reasoning": int(reasoning_tokens or 0),
        "total": int(input_tokens or 0) + int(output_tokens or 0),
    }


def session_id_from_name(path: Path) -> str:
    stem = path.stem
    if stem.startswith("model-io-sess_"):
        stem = stem[len("model-io-sess_"):]
    return stem or hashlib.sha1(str(path).encode()).hexdigest()[:16]


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    import json
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    yield event
    except OSError:
        return


def recent_files(pattern: str, *, limit: int, root: str | os.PathLike[str] = "") -> list[Path]:
    base = Path(root).expanduser() if root else None
    files = list(base.glob(pattern)) if base else []
    files = [p for p in files if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]
