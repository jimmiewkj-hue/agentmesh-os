"""Trae / Trae CN conversation reader.

Trae stores the full conversation in a SQLCipher database at
%APPDATA%/<Trae|Trae CN>/ModularData/ai-agent/database.db. The page format and
the fixed build-wide key were verified on this machine and are documented in
D:\\zcode\\trae-decrypt\\README.md; the control plane only performs read-only
decryption into a local cache.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..util import read_json, sha1_hex, write_json
from .common import (
    ParsedMessage, ParsedSession, project_matches, sanitize_text, summarize,
    text_from_blocks, token_totals,
)

DB_KEY = "3605f6691095a993f03d5009c918352ef5be31ae31e8f000212b81ff058da773"
PAGE_SIZE = 4096
RESERVE = 80
PLAIN_LEN = PAGE_SIZE - RESERVE
SALT_LEN = 16
MAGIC = b"SQLite format 3\x00"


class TraeDecryptError(RuntimeError):
    pass


def _key_bytes() -> bytes:
    return bytes.fromhex(DB_KEY)


def decrypt_database(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    key = _key_bytes()
    source = Path(src)
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as fh, target.open("wb") as out:
        first = True
        while True:
            page = fh.read(PAGE_SIZE)
            if not page:
                break
            if len(page) != PAGE_SIZE:
                break  # torn tail while the agent is writing
            if first:
                content_len = PLAIN_LEN - SALT_LEN
                ciphertext = page[SALT_LEN:SALT_LEN + content_len]
                iv = page[PLAIN_LEN:PLAIN_LEN + 16]
            else:
                content_len = PLAIN_LEN
                ciphertext = page[0:content_len]
                iv = page[PLAIN_LEN:PLAIN_LEN + 16]
            if len(ciphertext) % 16 != 0 or len(iv) != 16:
                raise TraeDecryptError("Trae 数据库页布局异常")
            decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
            plain = decryptor.update(ciphertext) + decryptor.finalize()
            if first:
                if not _header_ok(plain[:8]):
                    raise TraeDecryptError("Trae 数据库解密失败：页头校验不通过（密钥或格式不符）")
                out.write(MAGIC)
                first = False
            out.write(plain)
            out.write(b"\x00" * RESERVE)


def _header_ok(head: bytes) -> bool:
    if len(head) < 8:
        return False
    return (head[0] == 0x10 and head[1] == 0x00 and head[2] in (1, 2)
            and head[4] == 0x50 and head[5] == 0x40 and head[6] == 0x20 and head[7] == 0x20)


class TraeStore:
    def __init__(self, db_path: str | Path, *, cache_dir: str | Path | None = None):
        self.db_path = Path(db_path)
        self.cache_dir = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir())
        self.cache_path = self.cache_dir / f"agentloop-trae-{sha1_hex(str(self.db_path).lower())[:12]}.db"
        self.meta_path = self.cache_path.with_suffix(".meta.json")
        self.conn: sqlite3.Connection | None = None

    def open(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise TraeDecryptError(f"Trae 数据库不存在: {self.db_path}")
        st = self.db_path.stat()
        plaintext = False
        try:
            with self.db_path.open("rb") as fh:
                plaintext = fh.read(16) == MAGIC
        except OSError:
            plaintext = False
        if not plaintext:
            meta = read_json(self.meta_path, {}) or {}
            if not self.cache_path.exists() or meta.get("size") != st.st_size or meta.get("mtime_ns") != st.st_mtime_ns:
                tmp = self.cache_path.with_suffix(".tmp")
                decrypt_database(self.db_path, tmp)
                os.replace(tmp, self.cache_path)
                write_json(self.meta_path, {"size": st.st_size, "mtime_ns": st.st_mtime_ns})
            source = self.cache_path
        else:
            source = self.db_path
        conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=20)
        conn.row_factory = sqlite3.Row
        self.conn = conn
        return conn

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _text_from_json(value: Any, *, depth: int = 0) -> str:
    """Extract human text from Trae's several content JSON shapes."""
    if depth > 8:
        return ""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        if text.startswith("{") or text.startswith("["):
            try:
                return _text_from_json(json.loads(text), depth=depth + 1)
            except json.JSONDecodeError:
                return text
        return text
    if isinstance(value, list):
        parts = [_text_from_json(item, depth=depth + 1) for item in value]
        return "\n".join(p for p in parts if p).strip()
    if isinstance(value, dict):
        for key in ("text", "text_content", "content", "thought", "summary", "value"):
            if key in value:
                text = _text_from_json(value[key], depth=depth + 1)
                if text:
                    return text
        # raw message shapes
        if "raw_messages" in value:
            return ""
        parts = []
        for key in ("messages", "blocks", "parts"):
            if key in value:
                text = _text_from_json(value[key], depth=depth + 1)
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return ""


def _content_tables_text(conn: sqlite3.Connection, message_type: str, message_id: str) -> str:
    candidates = []
    if message_type == "task":
        candidates = ["chat_message_task"]
    elif message_type == "chat":
        candidates = ["chat_message_chat"]
    candidates += ["chat_message_general", "chat_message_chat", "chat_message_task"]
    for table in candidates:
        if not _table_exists(conn, table):
            continue
        row = conn.execute(
            f"SELECT content FROM {table} WHERE message_id=? "
            "AND COALESCE(deleted_at,'0')='0' ORDER BY id DESC LIMIT 1",
            (message_id,),
        ).fetchone()
        if row and row["content"]:
            text = _text_from_json(row["content"])
            if text:
                return text
    return ""


def _parsed_query(context: str) -> str:
    if not context:
        return ""
    try:
        data = json.loads(context)
    except json.JSONDecodeError:
        return ""
    return sanitize_text(data.get("parsed_query") or "")


def _history_windows(conn: sqlite3.Connection, session_id: str) -> dict[str, dict[str, Any]]:
    """Return latest history window per assistant message id."""
    if not _table_exists(conn, "history_v2"):
        return {}
    rows = conn.execute(
        "SELECT rowid AS rowid, COALESCE(message_id,'') AS message_id, "
        "COALESCE(messages,'') AS messages, COALESCE(token_usage,0) AS token_usage, "
        "COALESCE(created_at,'0') AS created_at FROM history_v2 "
        "WHERE session_id=? ORDER BY rowid",
        (session_id,),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        mid = str(row["message_id"] or "")
        payload = None
        try:
            payload = json.loads(row["messages"] or "")
        except json.JSONDecodeError:
            payload = None
        raw = (payload or {}).get("raw_messages") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            raw = []
        assistant_text = ""
        reasoning = ""
        calls: list[dict[str, Any]] = []
        tool_texts: list[str] = []
        for message in raw:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "")
            if role == "assistant":
                text, reason = text_from_blocks(message.get("content"))
                if text:
                    assistant_text = text
                if reason:
                    reasoning = reason
                for call in message.get("tool_calls") or []:
                    if isinstance(call, dict):
                        fn = call.get("function") or {}
                        calls.append({
                            "id": str(call.get("id") or ""),
                            "name": str(fn.get("name") or call.get("name") or ""),
                            "input": fn.get("arguments") if isinstance(fn.get("arguments"), dict) else {},
                        })
            elif role == "tool":
                text, _ = text_from_blocks(message.get("content"))
                if text:
                    tool_texts.append(text)
        out[mid or f"row{row['rowid']}"] = {
            "assistant_text": assistant_text, "reasoning": reasoning, "tool_calls": calls,
            "tool_texts": tool_texts, "token_usage": int(row["token_usage"] or 0),
            "created_at": str(row["created_at"] or "0"),
        }
    return out


def _sec_to_iso(value: str | int) -> str:
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    from ..util import iso_from_epoch_ms
    return iso_from_epoch_ms(seconds * 1000) or ""


def iter_sessions(db_path: str | Path, *, project_root: str = "",
                  limit: int = 50, cache_dir: str | Path | None = None) -> Iterable[ParsedSession]:
    store = TraeStore(db_path, cache_dir=cache_dir)
    try:
        conn = store.open()
        rows = conn.execute(
            "SELECT s.session_id, COALESCE(s.created_at,'0') AS created_at, "
            "COALESCE(s.updated_at,'0') AS updated_at, "
            "COALESCE(p.absolute_path,'') AS absolute_path FROM chat_session s "
            "LEFT JOIN project p ON p.project_id=s.project_id ORDER BY s.id DESC"
        ).fetchall()
        out: list[ParsedSession] = []
        for row in rows:
            if len(out) >= limit:
                break
            cwd = str(row["absolute_path"] or "")
            if project_root and cwd and not project_matches(cwd, project_root):
                continue
            if project_root and not cwd:
                continue
            session = _read_session(conn, str(row["session_id"]), cwd,
                                    _sec_to_iso(row["created_at"]), _sec_to_iso(row["updated_at"]))
            if session is not None:
                session.source_path = str(db_path)
                out.append(session)
        return out
    finally:
        store.close()


def _read_session(conn: sqlite3.Connection, session_id: str, cwd: str,
                  started: str, ended: str) -> ParsedSession | None:
    history = _history_windows(conn, session_id)
    messages: list[ParsedMessage] = []
    usage_total = 0
    title = ""
    model = ""

    if _table_exists(conn, "chat_message"):
        rows = conn.execute(
            "SELECT rowid AS rowid, COALESCE(message_id,'') AS message_id, "
            "COALESCE(message_role,'') AS message_role, COALESCE(created_at,'0') AS created_at, "
            "COALESCE(user_message_context,'') AS user_message_context, "
            "COALESCE(message_type,'') AS message_type "
            "FROM chat_message WHERE session_id=? ORDER BY rowid", (session_id,)
        ).fetchall()
    else:
        rows = []
    for row in rows:
        mid = str(row["message_id"] or "")
        role = str(row["message_role"] or "")
        ts = _sec_to_iso(row["created_at"])
        hist = history.get(mid, {})
        if role == "user":
            text = _content_tables_text(conn, str(row["message_type"] or ""), mid)
            if not text:
                text = _parsed_query(str(row["user_message_context"] or ""))
            if text:
                messages.append(ParsedMessage("user", text[:20000], ts))
                if not title:
                    title = text[:100]
        else:
            text = str(hist.get("assistant_text") or "")
            reasoning = str(hist.get("reasoning") or "")
            calls = hist.get("tool_calls") or []
            if not text:
                text = _content_tables_text(conn, str(row["message_type"] or ""), mid)
            if text or calls:
                messages.append(ParsedMessage("assistant", text[:20000], ts, calls, reasoning[:4000]))
            for tool_text in hist.get("tool_texts") or []:
                messages.append(ParsedMessage("tool", tool_text[:4000], ts))
    if not rows:
        for mid, hist in history.items():
            text = str(hist.get("assistant_text") or "")
            if text:
                messages.append(ParsedMessage("assistant", text[:20000], _sec_to_iso(hist.get("created_at", "0")),
                                              hist.get("tool_calls") or [], str(hist.get("reasoning") or "")[:4000]))
    # Token usage: each message's final window, summed.
    for mid, hist in history.items():
        usage_total += int(hist.get("token_usage") or 0)
    if not messages:
        return None
    return ParsedSession(
        session_id=session_id, source_path=str(conn.execute("PRAGMA database_list").fetchone()[2]),
        title=title, cwd=cwd, started_at=started, ended_at=ended, model=model,
        messages=messages,
        token_usage=token_totals(input_tokens=usage_total),
        fingerprint=sha1_hex(f"trae:{session_id}:{ended}"),
    )


def session_summary(session: ParsedSession) -> dict[str, Any]:
    return summarize(session)
