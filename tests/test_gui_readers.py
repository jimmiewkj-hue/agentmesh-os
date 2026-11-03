from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from agentloop_hub.gui.profiles import qoder_cn_profile, workbuddy_profile
from agentloop_hub.gui.readers import QoderCnReader, WorkBuddyReader


def _event(kind: str, **payload) -> str:
    return json.dumps({"type": kind, **payload}, ensure_ascii=False)


def test_workbuddy_reader_waits_for_final_turn(tmp_path):
    root = tmp_path / "projects" / "case"
    root.mkdir(parents=True)
    transcript = root / "sess.jsonl"
    marker = "AGENTMESH_GUI_MARKER_TEST"
    initial = [
        _event("message", role="user", sessionId="sess",
               content=[{"type": "input_text", "text": f"task {marker}"}]),
        _event("message", role="assistant", status="completed", sessionId="sess",
               providerData={"model": "glm-5.3-flash"},
               content=[{"type": "output_text", "text": "step one"}]),
        _event("function_call", name="Read", sessionId="sess", arguments={"file_path": "calc.py"}),
        _event("function_call_result", name="Read", sessionId="sess", status="completed",
               output={"type": "text", "text": "ok"}),
    ]
    transcript.write_text("\n".join(initial) + "\n", encoding="utf-8")
    db = tmp_path / "workbuddy.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, status TEXT)")
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?)", ("sess", "glm-5.3-flash", "working"))
    conn.commit()
    conn.close()

    def append_final() -> None:
        time.sleep(0.6)
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(_event("message", role="assistant", status="completed", sessionId="sess",
                                providerData={"model": "glm-5.3-flash",
                                              "usage": {"inputTokens": 10, "outputTokens": 5,
                                                        "totalTokens": 15}},
                                content=[{"type": "output_text", "text": "final answer"}]) + "\n")

    thread = threading.Thread(target=append_final)
    thread.start()
    reader = WorkBuddyReader(
        workbuddy_profile(),
        {"data_path": str(tmp_path / "projects"), "session_db": str(db),
         "poll_seconds": 0.1, "quiet_seconds": 0.15},
    )
    reply = reader.wait_reply(marker, started_at=time.time() - 1, timeout=5,
                              stall_seconds=3, expected_model="glm-5.3-flash")
    thread.join(timeout=2)
    assert reply.session_id == "sess"
    assert reply.model == "glm-5.3-flash"
    assert "step one" in reply.text
    assert "final answer" in reply.text
    assert reply.usage["total_tokens"] == 15
    assert len(reply.tool_calls) == 1


def test_workbuddy_reader_stall_state_is_per_transcript(tmp_path):
    root = tmp_path / "projects" / "case"
    root.mkdir(parents=True)
    marker = "AGENTMESH_GUI_MARKER_QUIET"
    done = root / "sess-a.jsonl"
    done.write_text("\n".join([
        _event("message", role="user", sessionId="sess-a",
               content=[{"type": "input_text", "text": f"task {marker}"}]),
        _event("message", role="assistant", status="completed", sessionId="sess-a",
               providerData={"model": "glm-5.3-flash"},
               content=[{"type": "output_text", "text": "final answer"}]),
    ]) + "\n", encoding="utf-8")
    busy = root / "sess-b.jsonl"
    busy.write_text(_event("message", role="user", sessionId="sess-b",
                           content=[{"type": "input_text", "text": "other"}]) + "\n",
                    encoding="utf-8")
    db = tmp_path / "workbuddy.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, status TEXT)")
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?)", ("sess-a", "glm-5.3-flash", "working"))
    conn.commit()
    conn.close()

    stop = threading.Event()

    def grow():  # an unrelated transcript keeps changing during the wait
        while not stop.is_set():
            with busy.open("a", encoding="utf-8") as handle:
                handle.write(_event("message", role="user", sessionId="sess-b",
                                    content=[{"type": "input_text", "text": "tick"}]) + "\n")
            time.sleep(0.05)

    thread = threading.Thread(target=grow)
    thread.start()
    reader = WorkBuddyReader(
        workbuddy_profile(),
        {"data_path": str(tmp_path / "projects"), "session_db": str(db),
         "poll_seconds": 0.05, "quiet_seconds": 0.2},
    )
    try:
        reply = reader.wait_reply(marker, started_at=time.time() - 1, timeout=5,
                                  stall_seconds=10, expected_model="glm-5.3-flash")
    finally:
        stop.set()
        thread.join(timeout=2)
    assert reply.transcript_path == str(done)
    assert reply.text == "final answer"


def test_qoder_reader_uses_answering_model_not_trailing_config(tmp_path):
    root = tmp_path / "projects" / "J--agentmesh-e2e-q1-20261103"
    root.mkdir(parents=True)
    marker = "AGENTMESH_GUI_MARKER_QODER"
    transcript = root / "sess.jsonl"
    transcript.write_text("\n".join([
        _event("user", sessionId="sess",
               message={"role": "user",
                        "content": [{"type": "text", "text": f"task {marker}"}]}),
        _event("assistant", sessionId="sess",
               message={"role": "assistant", "model": "dfmodel", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "QODER_DF_OK"}]}),
        _event("runtime-config", sessionId="sess", model="auto"),
    ]) + "\n", encoding="utf-8")
    reader = QoderCnReader(
        qoder_cn_profile(),
        {"data_path": str(tmp_path / "projects"), "poll_seconds": 0.1},
    )
    reply = reader.wait_reply(marker, started_at=time.time() - 1, timeout=5,
                              stall_seconds=3, expected_model="dfmodel")
    assert reply.session_id == "sess"
    assert reply.model == "dfmodel"
    assert reply.text == "QODER_DF_OK"
