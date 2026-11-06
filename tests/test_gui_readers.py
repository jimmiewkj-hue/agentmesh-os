from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from agentloop_hub.control import trae_interface as trae
from agentloop_hub.gui.profiles import qoder_cn_profile, trae_cn_profile, workbuddy_profile
from agentloop_hub.gui.readers import QoderCnReader, WorkBuddyReader
from agentloop_hub.gui.trae_reader import TraeReader


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


def test_reader_candidates_are_not_crowded_out_by_utility_transcripts(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    marker = "AGENTMESH_GUI_MARKER_NOT_CROWDED"
    marked = root / "marked.jsonl"
    marked.write_text("\n".join([
        _event("user", sessionId="marked",
               message={"role": "user",
                        "content": [{"type": "text", "text": f"task {marker}"}]}),
        _event("assistant", sessionId="marked",
               message={"role": "assistant", "model": "dfmodel", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "OK"}]}),
    ]) + "\n", encoding="utf-8")
    now = time.time()
    os.utime(marked, (now - 1, now - 1))
    for index in range(30):  # utility sessions are newer than the real chat
        noise = root / f"noise-{index}.jsonl"
        noise.write_text(_event("user", sessionId=f"n{index}",
                                message={"role": "user",
                                         "content": [{"type": "text", "text": "tick"}]}) + "\n",
                        encoding="utf-8")
        os.utime(noise, (now + index * 0.01, now + index * 0.01))
    reader = QoderCnReader(
        qoder_cn_profile(),
        {"data_path": str(root), "poll_seconds": 0.05},
    )
    reply = reader.wait_reply(marker, started_at=now - 2, timeout=5,
                              stall_seconds=5, expected_model="dfmodel")
    assert reply.session_id == "marked"


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


def test_trae_reader_ignores_echoed_prompt_and_waits_finished(tmp_path):
    marker = "AGENTMESH_GUI_MARKER_TRAE_TEST"

    def make_state(messages: int, status: str) -> trae.TraeState:
        session = {"sessionId": "s1", "assistant_task_status": status,
                   "messageIds": [f"m{i}" for i in range(messages)]}
        items = {
            "solo-lite-mode-state-map-u1": json.dumps({"code": {"session": session}}),
            "u1:AI.agent.model.recent_user_selection_by_agent_label": json.dumps(
                {"SOLO": {"modelId": "solo_agent_1__deepseek-v4.1-flash"}}
            ),
        }
        return trae.TraeState(app="trae_cn", uid="u1",
                              db=tmp_path / "state.vscdb", items=items)

    states = [make_state(2, "working"), make_state(4, "working"),
              make_state(4, "finished"), make_state(4, "finished"),
              make_state(4, "finished")]
    progress = (f"user prompt {marker}\n展示更多\n21:43\nTRAE\n"
                "已读取 1 个文件，执行 1 条命令，正在创建文件…")
    final = (f"user prompt {marker}\n展示更多\nTRAE\n"
             "```json\n{\"plan_version\": 1, \"goal\": \"g\", \"tasks\": []}\n```")
    texts = [f"user prompt {marker}", progress, final, final, final]

    def seq(values):
        box = {"i": 0}

        def next_value():
            value = values[min(box["i"], len(values) - 1)]
            box["i"] += 1
            return value
        return next_value

    reader = TraeReader(
        trae_cn_profile(),
        {"trae_poll_seconds": 0.01, "trae_stable_seconds": 0.01},
        state_reader=seq(states), text_reader=lambda: {"ok": True, "text": seq_texts()},
    )
    seq_texts = seq(texts)  # bound before the reader polls
    reply = reader.wait_reply(marker, started_at=time.time(), timeout=2,
                              stall_seconds=5, expected_model="deepseek-v4.1-flash")
    assert '"plan_version"' in reply.text
    assert "正在创建文件" not in reply.text
    assert reply.session_id == "s1"
    assert reply.events == 2


def test_workbuddy_active_sessions_use_latest_timestamp(tmp_path):
    db = tmp_path / "workbuddy.db"
    now_ms = int(time.time() * 1000)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, title TEXT, "
        "status TEXT, model TEXT, created_at INTEGER, updated_at INTEGER, "
        "last_activity_at INTEGER)")
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
        ("sess-active", "D:/work", "task", "working", "glm-5.3-flash",
         now_ms - 4 * 3600_000, now_ms, now_ms - 4 * 3600_000),
    )
    conn.commit()
    conn.close()
    reader = WorkBuddyReader(
        workbuddy_profile(),
        {"session_db": str(db), "data_path": str(tmp_path / "projects")},
    )
    active = reader.active_sessions(max_idle_seconds=900)
    assert [item["id"] for item in active] == ["sess-active"]
