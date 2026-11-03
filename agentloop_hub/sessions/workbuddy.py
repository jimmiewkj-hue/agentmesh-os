"""WorkBuddy transcript reader.

Source: ~/.workbuddy/projects/**/<uuid>.jsonl (+ <session>/subagents/agent-*.jsonl)
Event types verified on this machine: message, reasoning, function_call,
function_call_result, file-history-snapshot. Token usage lives in
providerData.usage (inputTokens/outputTokens + cached/reasoning details).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .common import (
    ParsedMessage, ParsedSession, file_fingerprint, parse_timestamp, project_matches,
    read_jsonl, sanitize_text, summarize, text_from_blocks, token_totals,
)


def iter_sessions(projects_dir: str | Path, *, project_root: str = "",
                  limit: int = 50) -> Iterable[ParsedSession]:
    base = Path(projects_dir)
    if not base.exists():
        return []
    files = [p for p in base.rglob("*.jsonl") if p.is_file()]
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


def _output_text(output: Any) -> str:
    if isinstance(output, str):
        return sanitize_text(output)
    if isinstance(output, dict):
        text, _ = text_from_blocks(output.get("content") if isinstance(output.get("content"), list) else [output],
                                   include_types=("text", "input_text", "output_text"))
        return text or sanitize_text(output.get("text") or "")
    if isinstance(output, list):
        text, _ = text_from_blocks(output)
        return text
    return ""


def read_session(path: Path) -> ParsedSession | None:
    session_id = path.stem
    cwd = ""
    started = ended = ""
    title = ""
    model = ""
    usage_in = usage_out = usage_cached = usage_reasoning = 0
    messages: list[ParsedMessage] = []

    for event in read_jsonl(path):
        etype = str(event.get("type") or "")
        cwd = cwd or str(event.get("cwd") or "")
        session_id = str(event.get("sessionId") or session_id)
        ts = parse_timestamp(event.get("timestamp"))
        if ts:
            started = started or ts
            ended = ts
        provider = event.get("providerData") or {}
        if isinstance(provider, dict):
            model = str(provider.get("model") or model)
            usage = provider.get("usage") or event.get("usage")
            if isinstance(usage, dict):
                usage_in += int(usage.get("inputTokens", usage.get("input_tokens", 0)) or 0)
                usage_out += int(usage.get("outputTokens", usage.get("output_tokens", 0)) or 0)
                for detail in usage.get("inputTokensDetails") or []:
                    if isinstance(detail, dict):
                        usage_cached += int(detail.get("cached_tokens", 0) or 0)
                for detail in usage.get("outputTokensDetails") or []:
                    if isinstance(detail, dict):
                        usage_reasoning += int(detail.get("reasoning_tokens", 0) or 0)
                raw = provider.get("rawUsage") or {}
                if isinstance(raw, dict) and not usage.get("inputTokens"):
                    usage_in += int(raw.get("prompt_tokens", 0) or 0)
                    usage_out += int(raw.get("completion_tokens", 0) or 0)
                    usage_cached += int(raw.get("prompt_cache_hit_tokens", 0) or 0)

        if etype == "message":
            role = str(event.get("role") or "")
            content = event.get("content")
            if role == "user":
                text, _ = text_from_blocks(content, include_types=("input_text", "text"))
                if not text:
                    continue
                messages.append(ParsedMessage("user", text, ts))
                if not title:
                    title = text[:100]
            elif role == "assistant":
                text, reasoning = text_from_blocks(content, include_types=("output_text", "text"))
                if text:
                    messages.append(ParsedMessage("assistant", text, ts, [], reasoning[:4000]))
                elif reasoning:
                    messages.append(ParsedMessage("assistant", "", ts, [], reasoning[:4000]))
        elif etype == "reasoning":
            raw = event.get("rawContent") or event.get("content")
            text, reasoning = text_from_blocks(raw, include_types=(), reasoning_types=("reasoning_text",))
            reasoning = reasoning or text
            if reasoning:
                messages.append(ParsedMessage("assistant", "", ts, [], reasoning[:4000]))
        elif etype == "function_call":
            call = {
                "id": str(event.get("callId") or event.get("id") or ""),
                "name": str(event.get("name") or ""),
                "input": event.get("arguments") if isinstance(event.get("arguments"), dict)
                         else _safe_json(event.get("arguments")),
            }
            messages.append(ParsedMessage("assistant", "", ts, [call]))
        elif etype == "function_call_result":
            text = _output_text(event.get("output"))
            if text:
                messages.append(ParsedMessage("tool", text[:4000], ts))
        elif etype == "file-history-snapshot":
            continue

    if not messages:
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


def session_summary(session: ParsedSession) -> dict[str, Any]:
    return summarize(session)
