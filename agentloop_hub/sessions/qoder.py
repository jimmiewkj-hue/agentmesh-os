"""Qoder / Qoder CN transcript reader.

Verified layouts:
  ~/.qoder/projects/<project>/transcript/<uuid>.jsonl      (international)
  ~/.qoder-cn/projects/<project>/<uuid>.jsonl              (CN, flat)
Both use the same event schema: user / assistant / attachment / last-prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .common import (
    ParsedSession, file_fingerprint, parse_timestamp, project_matches, read_jsonl,
    sanitize_text, summarize, text_from_blocks, tool_calls_from_blocks,
    tool_results_from_blocks, token_totals,
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
        if project_root and session.cwd and not project_matches(session.cwd, project_root):
            continue
        if project_root and not session.cwd:
            continue
        out.append(session)
    return out


def read_session(path: Path) -> ParsedSession | None:
    session_id = path.stem
    cwd = ""
    messages: list[dict[str, Any]] = []
    usage_in = usage_out = usage_cached = usage_reasoning = 0
    model = ""
    started = ended = ""
    title = ""
    found_any = False

    for event in read_jsonl(path):
        etype = str(event.get("type") or "")
        if event.get("isSidechain"):
            continue
        cwd = cwd or str(event.get("cwd") or "")
        if event.get("sessionId"):
            session_id = str(event["sessionId"])
        if etype == "workspace-directories" and not cwd:
            dirs = event.get("directories") or []
            if dirs:
                cwd = str(dirs[0])
        if etype == "runtime-config":
            model = str(event.get("model") or model)
            continue
        if etype == "last-prompt":
            title = title or sanitize_text(event.get("lastPrompt") or "")[:100]
            continue
        if etype not in ("user", "assistant"):
            continue
        found_any = True
        ts = parse_timestamp(event.get("timestamp"))
        if ts:
            started = started or ts
            ended = ts
        message = event.get("message") or {}
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or etype)
        content = message.get("content")
        if role == "user":
            text, _ = text_from_blocks(content, include_types=("text", "input_text"))
            if not text:
                human = event.get("humanInput") or {}
                if isinstance(human, dict):
                    text = sanitize_text(human.get("text") or "")
            results = tool_results_from_blocks(content)
            if text:
                messages.append({"role": "user", "text": text, "timestamp": ts})
                if not title:
                    title = text[:100]
            for result in results:
                messages.append({"role": "tool", "text": result[:2000], "timestamp": ts})
        else:
            text, reasoning = text_from_blocks(content)
            calls = tool_calls_from_blocks(content)
            if text or calls:
                messages.append({
                    "role": "assistant", "text": text, "timestamp": ts,
                    "tool_calls": calls, "reasoning": reasoning[:4000],
                })
            usage = message.get("usage") or {}
            if isinstance(usage, dict):
                usage_in += int(usage.get("input_tokens", usage.get("inputTokens", 0)) or 0)
                usage_out += int(usage.get("output_tokens", usage.get("outputTokens", 0)) or 0)
                usage_cached += int(usage.get("cache_read_input_tokens", usage.get("cacheReadTokens", 0)) or 0)
                usage_reasoning += int(usage.get("reasoning_tokens", 0) or 0)
            if message.get("model"):
                model = str(message["model"])

    if not found_any:
        return None
    session = ParsedSession(
        session_id=session_id, source_path=str(path), title=title, cwd=cwd,
        started_at=started, ended_at=ended, model=model,
        token_usage=token_totals(input_tokens=usage_in, output_tokens=usage_out,
                                 cached_tokens=usage_cached, reasoning_tokens=usage_reasoning),
        fingerprint=file_fingerprint(path),
    )
    for item in messages:
        session.messages.append(_to_parsed(item))
    if not session.title:
        first_user = next((m.text for m in session.messages if m.role == "user" and m.text), "")
        session.title = first_user[:100]
    return session


def _to_parsed(item: dict[str, Any]):
    from .common import ParsedMessage
    return ParsedMessage(
        role=item["role"], text=item.get("text", ""), timestamp=item.get("timestamp", ""),
        tool_calls=item.get("tool_calls") or [], reasoning=item.get("reasoning", ""),
    )


def session_summary(session: ParsedSession) -> dict[str, Any]:
    return summarize(session)
