"""OpenCode session reader.

Source: ~/.local/share/opencode/opencode.db (SQLite; verified schema)
Tables: session, session_message (new), message + part (legacy), project.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .common import (
    ParsedMessage, ParsedSession, file_fingerprint, iso_from_epoch_ms, project_matches,
    sanitize_text, summarize, text_from_blocks, token_totals,
)


def _connect(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def iter_sessions(db_path: str | Path, *, project_root: str = "",
                  limit: int = 50) -> Iterable[ParsedSession]:
    path = Path(db_path)
    if not path.exists():
        return []
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT id, directory, title, time_created, time_updated FROM session "
            "ORDER BY time_updated DESC LIMIT ?", (max(limit * 2, limit),)
        ).fetchall()
    except sqlite3.DatabaseError:
        rows = []
    out: list[ParsedSession] = []
    for row in rows:
        if len(out) >= limit:
            break
        directory = str(row["directory"] or "")
        if project_root and not project_matches(directory, project_root):
            continue
        session = _read_session(conn, row)
        if session is not None:
            out.append(session)
    conn.close()
    return out


def _read_session(conn: sqlite3.Connection, row: sqlite3.Row) -> ParsedSession | None:
    sid = str(row["id"])
    cwd = str(row["directory"] or "")
    title = str(row["title"] or "")
    started = iso_from_epoch_ms(row["time_created"]) or ""
    ended = iso_from_epoch_ms(row["time_updated"]) or ""
    messages: list[ParsedMessage] = []
    usage_in = usage_out = usage_cached = usage_reasoning = 0
    model = ""

    try:
        sm_rows = conn.execute(
            "SELECT id, type, seq, time_created, data FROM session_message "
            "WHERE session_id=? ORDER BY COALESCE(seq, 0), time_created",
            (sid,),
        ).fetchall()
    except sqlite3.DatabaseError:
        sm_rows = []

    if sm_rows:
        for item in sm_rows:
            data = _json(item["data"])
            mtype = str(item["type"] or "")
            ts = iso_from_epoch_ms(item["time_created"]) or ""
            if mtype == "user":
                text = sanitize_text(data.get("text") or "")
                if text:
                    messages.append(ParsedMessage("user", text, ts))
                    if not title:
                        title = text[:100]
            elif mtype == "assistant":
                content = data.get("content") if isinstance(data.get("content"), list) else []
                text, reasoning = text_from_blocks(content)
                calls = []
                tool_messages: list[ParsedMessage] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool":
                        calls.append({
                            "id": str(block.get("id") or ""),
                            "name": str(block.get("name") or ""),
                            "input": (block.get("state") or {}).get("input") if isinstance(block.get("state"), dict) else {},
                        })
                        state = block.get("state") or {}
                        if isinstance(state, dict) and state.get("status") == "completed":
                            output = state.get("content")
                            out_text, _ = text_from_blocks(output) if isinstance(output, list) else ("", "")
                            if out_text:
                                tool_messages.append(ParsedMessage("tool", out_text[:4000], ts))
                if text or calls:
                    messages.append(ParsedMessage("assistant", text, ts, calls, reasoning[:4000]))
                messages.extend(tool_messages)
                mdl = data.get("model") or {}
                if isinstance(mdl, dict):
                    mid = str(mdl.get("id") or mdl.get("modelID") or "")
                    provider = str(mdl.get("providerID") or "")
                    model = f"{provider}/{mid}" if provider and mid else (mid or model)
                tokens = data.get("tokens") or {}
                if isinstance(tokens, dict):
                    usage_in += int(tokens.get("input", 0) or 0)
                    usage_out += int(tokens.get("output", 0) or 0)
                    usage_reasoning += int(tokens.get("reasoning", 0) or 0)
                    cache = tokens.get("cache") or {}
                    if isinstance(cache, dict):
                        usage_cached += int(cache.get("read", 0) or 0)
    else:
        try:
            msg_rows = conn.execute(
                "SELECT id, data FROM message WHERE session_id=? ORDER BY time_created", (sid,)
            ).fetchall()
        except sqlite3.DatabaseError:
            msg_rows = []
        for item in msg_rows:
            data = _json(item["data"])
            role = str(data.get("role") or "")
            try:
                parts = conn.execute(
                    "SELECT data FROM part WHERE message_id=? ORDER BY time_created", (item["id"],)
                ).fetchall()
            except sqlite3.DatabaseError:
                parts = []
            blocks = [_json(p["data"]) for p in parts]
            if role == "user":
                text, _ = text_from_blocks(blocks)
                if text:
                    messages.append(ParsedMessage("user", text, ""))
            elif role == "assistant":
                text, reasoning = text_from_blocks(blocks)
                calls = []
                for block in blocks:
                    if isinstance(block, dict) and block.get("type") == "tool":
                        calls.append({"id": str(block.get("callID") or ""),
                                      "name": str(block.get("tool") or ""),
                                      "input": block.get("state", {}).get("input", {})})
                if text or calls:
                    messages.append(ParsedMessage("assistant", text, "", calls, reasoning[:4000]))
                tokens = data.get("tokens") or {}
                if isinstance(tokens, dict):
                    usage_in += int(tokens.get("input", 0) or 0)
                    usage_out += int(tokens.get("output", 0) or 0)
                    usage_reasoning += int(tokens.get("reasoning", 0) or 0)
                    cache = tokens.get("cache") or {}
                    if isinstance(cache, dict):
                        usage_cached += int(cache.get("read", 0) or 0)

    if not messages:
        return None
    return ParsedSession(
        session_id=sid, source_path=str(conn.execute("PRAGMA database_list").fetchone()[2]),
        title=title, cwd=cwd, started_at=started, ended_at=ended, model=model,
        messages=messages,
        token_usage=token_totals(input_tokens=usage_in, output_tokens=usage_out,
                                 cached_tokens=usage_cached, reasoning_tokens=usage_reasoning),
        fingerprint=file_fingerprint(Path(conn.execute("PRAGMA database_list").fetchone()[2])),
    )


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def session_summary(session: ParsedSession) -> dict[str, Any]:
    return summarize(session)
