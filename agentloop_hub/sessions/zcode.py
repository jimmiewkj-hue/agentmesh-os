"""ZCode rollout reader.

Source: ~/.zcode/cli/rollout/model-io-sess_*.jsonl
Each line is one model request/response window containing the full request
message history and the assistant response, including token usage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .common import (
    ParsedSession, file_fingerprint, parse_timestamp, sanitize_text, summarize,
    token_totals,
)


def iter_sessions(rollout_dir: str | Path, *, project_root: str = "",
                  limit: int = 50) -> Iterable[ParsedSession]:
    base = Path(rollout_dir)
    if not base.exists():
        return []
    files = sorted([p for p in base.glob("model-io-sess_*.jsonl") if p.is_file()],
                   key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out = []
    for path in files:
        session = read_session(path, project_root=project_root)
        if session is not None:
            out.append(session)
    return out


def read_session(path: Path, *, project_root: str = "") -> ParsedSession | None:
    session_id = path.stem.removeprefix("model-io-sess_")
    messages: list[dict[str, Any]] = []
    seen_prompts: set[tuple[str, str]] = set()
    usage_in = usage_out = usage_cached = usage_reasoning = 0
    model = ""
    started = ended = ""
    title = ""
    found = False

    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            found = True
            session_id = str(event.get("sessionId") or session_id)
            turn = str(event.get("turnId") or event.get("requestId") or "")
            started_at = parse_timestamp(event.get("startedAt"))
            completed_at = parse_timestamp(event.get("completedAt"))
            if started_at:
                started = started or started_at
            if completed_at:
                ended = completed_at
            mdl = event.get("model")
            if isinstance(mdl, dict) and mdl.get("modelId"):
                model = str(mdl["modelId"])
            request = event.get("request") or {}
            request_messages = request.get("messages") if isinstance(request, dict) else None
            if isinstance(request_messages, list):
                user_text = ""
                for message in reversed(request_messages):
                    if not isinstance(message, dict):
                        continue
                    if str(message.get("role") or "") != "user":
                        continue
                    content = message.get("content")
                    if isinstance(content, str):
                        user_text = sanitize_text(content)
                    elif isinstance(content, list):
                        parts = []
                        for block in content:
                            if isinstance(block, dict) and str(block.get("type")) in ("text", "input_text"):
                                parts.append(str(block.get("text") or ""))
                        user_text = sanitize_text("\n".join(parts))
                    break
                key = (turn, user_text)
                if user_text and key not in seen_prompts:
                    seen_prompts.add(key)
                    messages.append({"role": "user", "text": user_text, "timestamp": started_at})
                    if not title:
                        title = user_text[:100]
            response = event.get("response") or {}
            if isinstance(response, dict):
                text = sanitize_text(response.get("text") or "")
                calls = []
                for call in response.get("toolCalls") or []:
                    if isinstance(call, dict):
                        calls.append({"id": str(call.get("id") or ""),
                                      "name": str(call.get("name") or ""),
                                      "input": call.get("input") or {}})
                if text or calls:
                    messages.append({"role": "assistant", "text": text,
                                     "timestamp": completed_at, "tool_calls": calls})
                usage = response.get("usage") or {}
                if isinstance(usage, dict):
                    usage_in += int(usage.get("inputTokens", 0) or 0)
                    usage_out += int(usage.get("outputTokens", 0) or 0)
                    usage_cached += int(usage.get("cacheReadTokens", 0) or 0)
                    usage_reasoning += int(usage.get("reasoningTokens", 0) or 0)
    if not found:
        return None
    session = ParsedSession(
        session_id=session_id, source_path=str(path), title=title, cwd="",
        started_at=started, ended_at=ended, model=model,
        token_usage=token_totals(input_tokens=usage_in, output_tokens=usage_out,
                                 cached_tokens=usage_cached, reasoning_tokens=usage_reasoning),
        fingerprint=file_fingerprint(path),
    )
    for item in messages:
        session.add_text(item["role"], item.get("text", ""), item.get("timestamp", ""),
                         item.get("tool_calls") or [])
    return session


def session_summary(session: ParsedSession) -> dict[str, Any]:
    return summarize(session)
