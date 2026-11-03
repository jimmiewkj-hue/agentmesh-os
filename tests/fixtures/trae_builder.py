"""Build a small Trae-shaped SQLCipher database for tests.

This is the exact inverse of agentloop_hub.sessions.trae.decrypt_database and
lets the reader be tested without touching the real 200-800 MB databases.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY = bytes.fromhex("3605f6691095a993f03d5009c918352ef5be31ae31e8f000212b81ff058da773")
PAGE = 4096
RESERVE = 80
PLAIN = PAGE - RESERVE
SALT = 16
MAGIC = b"SQLite format 3\x00"


def create_plain_db(path: Path, *, cwd: str) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE chat_session (id INTEGER PRIMARY KEY, session_id TEXT, project_id TEXT,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE project (project_id TEXT PRIMARY KEY, absolute_path TEXT);
        CREATE TABLE chat_message (id INTEGER PRIMARY KEY, session_id TEXT, message_id TEXT,
            message_role TEXT, created_at TEXT, user_message_context TEXT, message_type TEXT,
            deleted_at TEXT DEFAULT '0');
        CREATE TABLE chat_message_general (id INTEGER PRIMARY KEY, message_id TEXT,
            content TEXT, deleted_at TEXT DEFAULT '0');
        CREATE TABLE chat_message_task (id INTEGER PRIMARY KEY, message_id TEXT,
            content TEXT, summary TEXT, deleted_at TEXT DEFAULT '0');
        CREATE TABLE history_v2 (id INTEGER PRIMARY KEY, session_id TEXT, message_id TEXT,
            messages TEXT, token_usage INTEGER, created_at TEXT);
    """)
    conn.execute("INSERT INTO project VALUES('p1', ?)", (cwd,))
    conn.execute("INSERT INTO chat_session VALUES(1, 'sessA', 'p1', '1742281200', '1742281400')")
    conn.execute("INSERT INTO chat_message(session_id, message_id, message_role, created_at, "
                 "message_type) VALUES('sessA','m1','user','1742281200','chat')")
    conn.execute("INSERT INTO chat_message_general(message_id, content) VALUES(?,?)",
                 ("m1", '[{"type":"text","text_content":"please fix calc"}]'))
    conn.execute("INSERT INTO chat_message(session_id, message_id, message_role, created_at, "
                 "message_type) VALUES('sessA','m2','assistant','1742281400','task')")
    conn.execute("INSERT INTO history_v2(session_id, message_id, messages, token_usage, created_at) "
                 "VALUES(?,?,?,?,?)",
                 ("sessA", "m2", '{"raw_messages":[{"role":"assistant","content":'
                  '[{"type":"text","text":"fixed it"}],"tool_calls":[{"id":"tc1",'
                  '"type":"function","function":{"name":"Read","arguments":'
                  '"{\\"path\\":\\"calc.py\\"}"}}]},{"role":"tool","tool_call_id":"tc1",'
                  '"content":"[{\\"type\\":\\"text\\",\\"text\\":\\"body\\"}]"}]}', 900,
                  "1742281300"))
    conn.commit()
    conn.close()
    return path


def encrypt_db(src: Path, dst: Path) -> Path:
    with src.open("rb") as fh, dst.open("wb") as out:
        first = True
        while True:
            page = fh.read(PAGE)
            if not page:
                break
            if len(page) != PAGE:
                raise ValueError("plaintext database page is not 4096 bytes")
            iv = os.urandom(16)
            if first:
                page = bytearray(page)
                page[20] = 0x50  # SQLCipher reserved-space byte (80)
                page = bytes(page)
                content = page[16:PAGE - RESERVE]
                salt = os.urandom(16)
                enc = Cipher(algorithms.AES(KEY), modes.CBC(iv)).encryptor()
                ciphertext = enc.update(content) + enc.finalize()
                out.write(salt)
                out.write(ciphertext)
                out.write(iv)
                out.write(os.urandom(64))
                first = False
            else:
                content = page[0:PLAIN]
                enc = Cipher(algorithms.AES(KEY), modes.CBC(iv)).encryptor()
                ciphertext = enc.update(content) + enc.finalize()
                out.write(ciphertext)
                out.write(iv)
                out.write(os.urandom(64))
    return dst
