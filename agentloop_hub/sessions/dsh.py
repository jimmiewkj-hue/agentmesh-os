"""DeepSeek Harness (DSH) session reader for *.jsonl.zstd event streams.

Source: ~/.dsh/sessions/**/session.jsonl.zstd
Event types verified on this machine include session, session/title,
model/selection, user/message, assistant/message, tool/call, tool/result.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .common import (
    ParsedMessage, ParsedSession, file_fingerprint, parse_timestamp, project_matches,
    sanitize_text, summarize, text_from_blocks, token_totals,
)


def iter_sessions(sessions_dir: str | Path, *, project_root: str = "",
                  limit: int = 50) -> Iterable[ParsedSession]:
    base = Path(sessions_dir)
    if not base.exists():
        return []
    files = [p for p in base.rglob("*.jsonl.zstd") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[ParsedSession] = []
    for path in files:
        if len(out) >= limit:
            break
        session = read_session(path)
        if session is None:
            continue
        if project_root and not session.cwd:
            continue
        if project_root and not project_matches(session.cwd, project_root):
            continue
        out.append(session)
    return out


def _read_events(path: Path) -> Iterable[dict[str, Any]]:
    try:
        import zstandard
    except ImportError as exc:
        raise RuntimeError("读取 DSH 会话需要安装 zstandard") from exc
    try:
        with path.open("rb") as fh:
            with zstandard.ZstdDecompressor().stream_reader(fh) as reader:
                # zstd's stream reader does not support line iteration; read the
                # decompressed payload and split it deterministically.
                payload = reader.read()
                for line in payload.decode("utf-8", "replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        yield event
    except OSError:
        return


def read_session(path: Path) -> ParsedSession | None:
    session_id = path.parent.name
    cwd = ""
    title = ""
    model = ""
    started = ended = ""
    messages: list[ParsedMessage] = []
    usage_in = usage_out = usage_cached = usage_reasoning = 0
    found = False

    for event in _read_events(path):
        found = True
        etype = str(event.get("type") or "")
        data = event.get("data") or {}
        ts = parse_timestamp(event.get("time") or data.get("time"))
        if ts:
            started = started or ts
            ended = ts
        if etype == "session":
            session_id = str(event.get("id") or session_id)
            cwd = str(event.get("cwd") or cwd)
            created = event.get("createdAt")
            if created:
                started = parse_timestamp(created) or started
        elif etype == "session/title":
            title = sanitize_text(data.get("title") or title)[:100]
        elif etype == "model/selection":
            model = str(data.get("model") or model)
        elif etype == "user/message":
            text, _ = text_from_blocks(data.get("content"), include_types=("text", "input_text"))
            if text:
                messages.append(ParsedMessage("user", text, ts))
                if not title:
                    title = text[:100]
        elif etype == "assistant/message":
            message = data.get("message") or {}
            content = message.get("content")
            text, reasoning = text_from_blocks(content)
            calls = []
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool-call":
                    calls.append({
                        "id": str(block.get("toolCallId") or ""),
                        "name": str(block.get("toolName") or ""),
                        "input": block.get("input") or {},
                    })
            if text:
                messages.append(ParsedMessage("assistant", text, ts, calls, reasoning[:4000]))
            elif calls:
                messages.append(ParsedMessage("assistant", "", ts, calls, reasoning[:4000]))
        elif etype == "tool/call":
            call = {
                "id": str(data.get("callId") or ""),
                "name": str(data.get("name") or ""),
                "input": data.get("arguments") if isinstance(data.get("arguments"), dict)
                         else _safe_json(data.get("arguments")),
            }
            if call["name"]:
                messages.append(ParsedMessage("assistant", "", ts, [call]))
        elif etype == "tool/result":
            message = data.get("message") or {}
            text = _tool_result_text(message.get("content"))
            if text:
                messages.append(ParsedMessage("tool", text[:4000], ts))
        elif etype in ("assistant/chunk", "assistant/attempt"):
            chunk = data.get("chunk") or {}
            usage = None
            if isinstance(chunk, dict) and chunk.get("type") == "usage":
                usage = chunk.get("usage")
            if not usage:
                for item in data.get("stream") or []:
                    if isinstance(item, dict):
                        inner = item.get("chunk") or {}
                        if isinstance(inner, dict) and inner.get("type") == "usage":
                            usage = inner.get("usage")
                            break
            if isinstance(usage, dict):
                usage_in += int(usage.get("inputTokens", 0) or 0)
                usage_out += int(usage.get("outputTokens", 0) or 0)
                usage_cached += int(usage.get("cachedTokens", 0) or 0)
                usage_reasoning += int(usage.get("reasoningTokens", 0) or 0)

    if not found or not messages:
        return None
    session = ParsedSession(
        session_id=session_id, source_path=str(path), title=title, cwd=cwd,
        started_at=started, ended_at=ended, model=model,
        token_usage=token_totals(input_tokens=usage_in, output_tokens=usage_out,
                                 cached_tokens=usage_cached, reasoning_tokens=usage_reasoning),
        fingerprint=file_fingerprint(path),
    )
    session.messages = messages
    return session


def _safe_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[:2000]
    return value


def _tool_result_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool-result":
            nested = block.get("content")
            if isinstance(nested, list):
                parts.extend(_tool_result_text(nested))
            elif isinstance(nested, str):
                parts.append(nested)
        elif block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return sanitize_text("\n".join(p for p in parts if p))


def session_summary(session: ParsedSession) -> dict[str, Any]:
    return summarize(session)
