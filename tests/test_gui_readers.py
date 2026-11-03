from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from agentloop_hub.gui.profiles import workbuddy_profile
from agentloop_hub.gui.readers import WorkBuddyReader


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
