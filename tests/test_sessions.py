from __future__ import annotations

import json
import sqlite3
import zstandard
from pathlib import Path

import pytest

from agentloop_hub.models import AgentRecord, Project
from agentloop_hub.sessions import qoder, zcode, workbuddy, dsh, opencode, trae
from agentloop_hub.sessions.common import project_matches, sanitize_text, summarize
from agentloop_hub.sessions.service import SessionImportService
from agentloop_hub.storage.db import Database
from agentloop_hub.storage.repo import Store
from fixtures.trae_builder import create_plain_db, encrypt_db


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_project_matches_and_sanitize():
    root = r"C:\proj\demo"
    assert project_matches(r"C:\proj\demo\sub", root)
    assert not project_matches(r"C:\other", root)
    text = "<system-reminder>x</system-reminder>\nreal request"
    assert sanitize_text(text) == "real request"


def test_qoder_reader_cn_and_intl(tmp_path):
    root = str(tmp_path / "project")
    base = [{
        "type": "workspace-directories", "sessionId": "q1", "directories": [root],
    }, {
        "type": "user", "sessionId": "q1", "uuid": "u1", "timestamp": "2026-01-01T00:00:00Z",
        "cwd": root, "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    }, {
        "type": "assistant", "sessionId": "q1", "uuid": "u2", "timestamp": "2026-01-01T00:01:00Z",
        "cwd": root,
        "message": {"role": "assistant", "model": "q-model", "content": [
            {"type": "thinking", "thinking": "think"},
            {"type": "text", "text": "answer"},
            {"type": "tool_use", "id": "c1", "name": "Read", "input": {"file": "a.txt"}},
        ], "usage": {"input_tokens": 10, "output_tokens": 5}},
    }, {
        "type": "user", "sessionId": "q1", "uuid": "u3", "timestamp": "2026-01-01T00:02:00Z",
        "cwd": root, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "body"},
        ]},
    }]
    text = "\n".join(json.dumps(x) for x in base)
    write(tmp_path / "projects" / "p" / "transcript" / "q1.jsonl", text)
    sessions = list(qoder.iter_sessions(tmp_path / "projects", project_root=root, limit=10))
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "q1"
    assert s.title == "hello"
    assert s.model == "q-model"
    assert s.token_usage["input"] == 10
    roles = [m.role for m in s.messages]
    assert roles == ["user", "assistant", "tool"]
    assert s.messages[1].tool_calls[0]["name"] == "Read"


def test_qoder_reader_skips_other_project(tmp_path):
    other = str(tmp_path / "other")
    line = json.dumps({"type": "user", "sessionId": "q2", "cwd": other,
                       "message": {"role": "user", "content": "x"}})
    write(tmp_path / "projects" / "p" / "q2.jsonl", line + "\n")
    sessions = list(qoder.iter_sessions(tmp_path / "projects", project_root=str(tmp_path / "project")))
    assert sessions == []


def test_zcode_reader(tmp_path):
    rows = [{
        "sessionId": "z1", "turnId": "t1", "model": {"modelId": "z-model"},
        "startedAt": "2026-01-01T00:00:00Z", "completedAt": "2026-01-01T00:01:00Z",
        "request": {"messages": [{"role": "user", "content": "hello"}]},
        "response": {"text": "hi", "toolCalls": [{"id": "c1", "name": "Bash", "input": {"command": "ls"}}],
                     "usage": {"inputTokens": 10, "outputTokens": 2, "cacheReadTokens": 3,
                               "reasoningTokens": 1, "totalTokens": 12}},
    }, {
        "sessionId": "z1", "turnId": "t2", "model": {"modelId": "z-model"},
        "startedAt": "2026-01-01T00:02:00Z", "completedAt": "2026-01-01T00:03:00Z",
        "request": {"messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "again"},
        ]},
        "response": {"text": "done", "usage": {"inputTokens": 20, "outputTokens": 4}},
    }]
    write(tmp_path / "rollout" / "model-io-sess_z1.jsonl",
          "\n".join(json.dumps(r) for r in rows))
    sessions = list(zcode.iter_sessions(tmp_path / "rollout", limit=5))
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "z1"
    users = [m.text for m in s.messages if m.role == "user"]
    assert users == ["hello", "again"]
    assert s.token_usage["input"] == 30
    assert any(m.tool_calls and m.tool_calls[0]["name"] == "Bash" for m in s.messages)


def test_workbuddy_reader(tmp_path):
    root = str(tmp_path / "project")
    rows = [
        {"type": "message", "role": "user", "timestamp": 1767225600000,
         "sessionId": "w1", "cwd": root,
         "content": [{"type": "input_text",
                      "text": "<system-reminder>noise</system-reminder>real ask"}]},
        {"type": "reasoning", "timestamp": 1767225601000, "sessionId": "w1", "cwd": root,
         "rawContent": [{"type": "reasoning_text", "text": "thinking"}]},
        {"type": "function_call", "timestamp": 1767225602000, "sessionId": "w1", "cwd": root,
         "name": "Read", "arguments": "{\"file\":\"a.txt\"}", "callId": "c1",
         "providerData": {"model": "wb-model", "usage": {
             "inputTokens": 10, "outputTokens": 2,
             "inputTokensDetails": [{"cached_tokens": 3}],
             "outputTokensDetails": [{"reasoning_tokens": 1}]}}},
        {"type": "function_call_result", "timestamp": 1767225603000, "sessionId": "w1", "cwd": root,
         "name": "Read", "callId": "c1", "status": "completed",
         "output": {"type": "text", "text": "body"}},
        {"type": "message", "role": "assistant", "timestamp": 1767225604000,
         "sessionId": "w1", "cwd": root,
         "content": [{"type": "output_text", "text": "done"}]},
    ]
    write(tmp_path / "projects" / "p" / "w1.jsonl",
          "\n".join(json.dumps(r) for r in rows))
    sessions = list(workbuddy.iter_sessions(tmp_path / "projects", project_root=root))
    assert len(sessions) == 1
    s = sessions[0]
    assert s.title == "real ask"
    assert s.model == "wb-model"
    assert s.token_usage["cached"] == 3
    assert any(m.tool_calls and m.tool_calls[0]["name"] == "Read" for m in s.messages)


def test_dsh_reader_zstd(tmp_path):
    root = str(tmp_path / "project")
    rows = [
        {"type": "session", "id": "session-d1", "createdAt": 1767225600000, "cwd": root},
        {"type": "session/title", "data": {"title": "demo"}},
        {"type": "model/selection", "data": {"model": "dsh-model"}},
        {"type": "user/message", "time": 1767225601000,
         "data": {"content": [{"type": "text", "text": "hello"}]}},
        {"type": "assistant/message", "time": 1767225602000,
         "data": {"message": {"role": "assistant",
                              "content": [{"type": "text", "text": "hi"}]}}},
        {"type": "tool/call", "time": 1767225603000,
         "data": {"callId": "c1", "name": "pwsh",
                  "arguments": "{\"command\":\"ls\"}"}},
        {"type": "tool/result", "time": 1767225604000,
         "data": {"message": {"content": [{"type": "tool-result",
                                           "content": [{"type": "text", "text": "out"}]}]}}},
        {"type": "assistant/chunk", "data": {"chunk": {"type": "usage",
                                                       "usage": {"inputTokens": 10,
                                                                 "outputTokens": 2}}}},
    ]
    raw = "\n".join(json.dumps(r) for r in rows).encode("utf-8")
    target = tmp_path / "sessions" / "p" / "session-d1"
    target.mkdir(parents=True)
    (target / "session.jsonl.zstd").write_bytes(
        zstandard.ZstdCompressor().compress(raw)
    )
    sessions = list(dsh.iter_sessions(tmp_path / "sessions", project_root=root))
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "session-d1"
    assert s.title == "demo"
    assert s.model == "dsh-model"
    assert s.token_usage["input"] == 10
    assert [m.role for m in s.messages][:4] == ["user", "assistant", "assistant", "tool"]


def make_opencode_db(path: Path, root: str) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE session(id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT,
            directory TEXT, title TEXT, time_created INTEGER, time_updated INTEGER);
        CREATE TABLE session_message(id TEXT PRIMARY KEY, session_id TEXT, type TEXT,
            seq INTEGER, time_created INTEGER, data TEXT);
    """)
    conn.execute("INSERT INTO session VALUES('o1','p',NULL,?,'open demo',1000,2000)", (root,))
    conn.execute("INSERT INTO session_message VALUES('m1','o1','user',1,1000,?)",
                 (json.dumps({"text": "hello"}),))
    conn.execute("INSERT INTO session_message VALUES('m2','o1','assistant',2,1500,?)",
                 (json.dumps({
                     "content": [
                         {"type": "reasoning", "text": "think"},
                         {"type": "text", "text": "hi"},
                         {"type": "tool", "id": "c1", "name": "bash",
                          "state": {"status": "completed", "input": {"command": "ls"},
                                    "content": [{"type": "text", "text": "out"}]}},
                     ],
                     "model": {"id": "oc-model", "providerID": "prov"},
                     "tokens": {"input": 10, "output": 2, "reasoning": 1,
                                "cache": {"read": 3}},
                     "cost": 0.01,
                 }),))
    conn.commit()
    conn.close()
    return path


def test_opencode_reader(tmp_path):
    root = str(tmp_path / "project")
    db = make_opencode_db(tmp_path / "opencode.db", root)
    sessions = list(opencode.iter_sessions(db, project_root=root))
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "o1"
    assert s.model == "prov/oc-model"
    assert s.token_usage["cached"] == 3
    assert [m.role for m in s.messages] == ["user", "assistant", "tool"]


def test_trae_decrypt_roundtrip(tmp_path):
    # Synthetic SQLCipher page layout: page 1 keeps the standard magic and a
    # valid header tail; both pages end with the 80 reserved zero bytes.
    page1 = bytearray(4096)
    page1[0:16] = b"SQLite format 3\x00"
    page1[16:24] = bytes.fromhex("1000020250402020")
    page1[20] = 0x50
    page2 = bytearray(4096)
    synthetic = tmp_path / "synthetic.db"
    synthetic.write_bytes(bytes(page1) + bytes(page2))
    encrypted = encrypt_db(synthetic, tmp_path / "database.db")
    decrypted = tmp_path / "decrypted.db"
    trae.decrypt_database(encrypted, decrypted)
    assert decrypted.read_bytes() == synthetic.read_bytes()


def test_trae_reader(tmp_path):
    root = str(tmp_path / "project")
    plain = create_plain_db(tmp_path / "plain.db", cwd=root)
    sessions = list(trae.iter_sessions(plain, project_root=root,
                                       cache_dir=tmp_path / "cache"))
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "sessA"
    assert s.title == "please fix calc"
    assert s.messages[0].text == "please fix calc"
    assistant = [m for m in s.messages if m.role == "assistant"]
    assert assistant and assistant[0].text == "fixed it"
    assert assistant[0].tool_calls[0]["name"] == "Read"
    assert s.token_usage["input"] == 900
    # A different project cwd is filtered out.
    other = create_plain_db(tmp_path / "other.db", cwd=str(tmp_path / "other"))
    assert list(trae.iter_sessions(other, project_root=root, cache_dir=tmp_path / "c2")) == []


def test_session_import_service_archives_and_filters(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    db = Database(tmp_path / "state.db")
    store = Store(db)
    project = Project(project_id="prj_1", name="p", root=str(project_root),
                      workspace_id="w", repository_id="r", worktree_id="t")
    store.save_project(project)
    agent = AgentRecord(agent_id="qoder", name="Qoder", product="Qoder", project_id="prj_1",
                        adapter_type="file_relay", access_level="L2",
                        adapter_config={"session_reader": "qoder",
                                        "data_path": str(tmp_path / "projects")})
    row = {
        "type": "user", "sessionId": "q1", "cwd": str(project_root),
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    }
    write(tmp_path / "projects" / "p" / "q1.jsonl", json.dumps(row) + "\n")
    other = dict(row, sessionId="q2", cwd=str(tmp_path / "other"))
    write(tmp_path / "projects" / "p" / "q2.jsonl", json.dumps(other) + "\n")
    service = SessionImportService(store, project)
    result = service.import_for_agent(agent, limit=10)
    assert result.imported == 1
    assert result.skipped == 1
    record = store.get_conversation("prj_1", "qoder", "q1")
    assert record and record["message_count"] == 1
    archive = json.loads(Path(record["raw_path"]).read_text(encoding="utf-8"))
    assert archive["messages"][0]["text"] == "hello"
    assert archive["summary"]["method"] == "deterministic"
