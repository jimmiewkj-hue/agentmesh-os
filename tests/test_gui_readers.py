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


def _trae_reader(tmp_path, **config):
    return TraeReader(trae_cn_profile(), config,
                      state_reader=lambda: trae.TraeState(
                          app="trae_cn", uid="u1",
                          db=tmp_path / "state.vscdb", items={}),
                      text_reader=lambda: {"ok": True, "text": ""})


def test_trae_receipt_accepts_attempt_result_file(tmp_path, monkeypatch):
    reader = _trae_reader(tmp_path)
    result_file = tmp_path / "result.json"
    result_file.write_text('{"task_id": "T1", "summary": "done"}', encoding="utf-8")
    reader.completion_path = str(result_file)
    receipt = reader.has_marker("M1")
    assert receipt["ok"] is True
    assert receipt["source"] == "attempt-result-file"


def test_trae_receipt_invalid_result_file_falls_through(tmp_path, monkeypatch):
    reader = _trae_reader(tmp_path)
    result_file = tmp_path / "result.json"
    result_file.write_text("half-written {", encoding="utf-8")
    reader.completion_path = str(result_file)
    monkeypatch.setattr(trae, "trae_marker_in_transcript",
                        lambda *a, **k: {"found": False, "reason": "MARKER_NOT_FOUND"})
    receipt = reader.has_marker("M1")
    assert receipt["ok"] is False


def test_trae_receipt_accepts_marker_rendered_outside_composer(tmp_path, monkeypatch):
    reader = _trae_reader(tmp_path)
    monkeypatch.setattr(trae, "trae_marker_in_transcript",
                        lambda *a, **k: {"found": True, "outside": 1, "inside": 0,
                                         "page_id": "P1"})
    receipt = reader.has_marker("M1")
    assert receipt["ok"] is True
    assert receipt["source"] == "cdp-transcript"


def test_trae_receipt_rejects_marker_only_in_composer(tmp_path, monkeypatch):
    reader = _trae_reader(tmp_path)
    monkeypatch.setattr(trae, "trae_marker_in_transcript",
                        lambda *a, **k: {"found": False, "in_composer_only": True,
                                         "outside": 0, "inside": 1})
    receipt = reader.has_marker("M1")
    assert receipt["ok"] is False
    assert receipt["reason"] == "MARKER_ONLY_IN_COMPOSER"


def test_trae_receipt_unknown_without_any_evidence(tmp_path, monkeypatch):
    reader = _trae_reader(tmp_path)
    monkeypatch.setattr(trae, "trae_marker_in_transcript",
                        lambda *a, **k: {"found": False, "reason": "MARKER_NOT_FOUND"})
    receipt = reader.has_marker("M1")
    assert receipt["ok"] is False
    assert receipt["reason"] == "MARKER_NOT_FOUND"


def test_trae_marker_probe_scans_all_pages_and_pins_first(monkeypatch):
    from agentloop_hub.gui import cdp as cdp_module

    class FakeTransport:
        def __init__(self, url, *, origin="", timeout=8):
            self.page_id = url.rsplit("/", 1)[-1]

        def close(self):
            pass

    probes = {"P1": {"found": False, "inside": 1, "outside": 0},
              "P2": {"found": True, "inside": 0, "outside": 1}}
    seen: list[str] = []

    def fake_evaluate(transport, expression, timeout):
        seen.append(transport.page_id)
        assert "M1" in expression
        return probes[transport.page_id]

    monkeypatch.setattr(cdp_module, "_targets", lambda port: [
        {"type": "page", "id": pid, "webSocketDebuggerUrl": f"ws://fake/{pid}"}
        for pid in ("P1", "P2")])
    monkeypatch.setattr(cdp_module, "WebSocketTransport", FakeTransport)
    monkeypatch.setattr(cdp_module, "_evaluate", fake_evaluate)
    found = trae.trae_marker_in_transcript(9342, "M1", page_id="P2")
    assert found["found"] is True
    assert seen[0] == "P2"
    probes["P2"] = {"found": False}
    probes["P1"] = {"found": True, "outside": 1}
    found = trae.trae_marker_in_transcript(9342, "M1")
    assert found["found"] is True
    assert found["page_id"] == "P1"


def test_adapter_reconcile_accepts_trae_attempt_result(tmp_path):
    from agentloop_hub.adapters.gui import GuiAutomationAdapter
    from test_gui_adapter import make_agent

    agent = make_agent("trae_cn")
    agent.agent_id = "trae_cn"
    adapter = GuiAutomationAdapter(
        agent, config={"mode": "gui_automation", "gui_app": "trae_cn"})
    adapter._reader = _trae_reader(tmp_path)
    result_file = tmp_path / "attempt-result.json"
    result_file.write_text(json.dumps({
        "task_id": "T008", "agent_id": "trae_cn", "status": "completed",
        "summary": "评审完成",
    }), encoding="utf-8")
    attempt = {
        "attempt_id": "att_1",
        "binding": {"marker": "M1"},
        "result_path": str(result_file),
    }
    receipt = adapter.reconcile_submission(attempt)
    assert receipt["status"] == "accepted"
    assert receipt["evidence"]["source"] == "attempt-result-file"
    assert receipt["package"]["summary"] == "评审完成"
    assert adapter._reader.completion_path == str(result_file)


def _hollow_trae_state(tmp_path, messages=0, status=""):
    session = {"sessionId": "s1", "messageIds": [f"m{i}" for i in range(messages)]}
    if status:
        session["assistant_task_status"] = status
    items = {
        "solo-lite-mode-state-map-u1": json.dumps({"code": {"session": session}}),
        "u1:AI.agent.model.recent_user_selection_by_agent_label": json.dumps(
            {"SOLO": {"modelId": "solo_agent_1__deepseek-v4.1-flash"}}),
    }
    return trae.TraeState(app="trae_cn", uid="u1",
                          db=tmp_path / "state.vscdb", items=items)


def _seq_reader(values):
    box = {"i": 0}

    def next_value():
        value = values[min(box["i"], len(values) - 1)]
        box["i"] += 1
        return value
    return next_value


def test_trae_reader_fallback_when_global_state_is_hollow(tmp_path):
    """2026-11-11 real defect: messageIds=0 / no assistant_task_status while
    the window had finished; the reader must accept DOM + result-file proof."""
    marker = "AGENTMESH_GUI_MARKER_HOLLOW"
    state = _hollow_trae_state(tmp_path)
    result_file = tmp_path / "plan.json"
    result_file.write_text('{"plan_version": 1, "goal": "g", "tasks": []}',
                           encoding="utf-8")
    final = (f"user prompt {marker}\n展示更多\nTRAE 思考用时 3m 33s\n"
             "已创建 1 个文件\n```json\n{\"plan_version\": 1}\n```")
    texts = [f"user prompt {marker}", final, final, final]
    seq_texts = _seq_reader(texts)
    reader = TraeReader(
        trae_cn_profile(),
        {"trae_poll_seconds": 0.01, "trae_stable_seconds": 0.01},
        state_reader=lambda: state,
        text_reader=lambda: {"ok": True, "text": seq_texts()},
    )
    reader.completion_path = str(result_file)
    reply = reader.wait_reply(marker, started_at=time.time(), timeout=2,
                              stall_seconds=5, expected_model="deepseek-v4.1-flash")
    assert reply.session_id == "s1"
    assert "plan_version" in reply.text


def test_trae_reader_fallback_needs_evidence(tmp_path):
    from agentloop_hub.gui.base import GuiAutomationError
    marker = "AGENTMESH_GUI_MARKER_NOEVIDENCE"
    state = _hollow_trae_state(tmp_path)
    text = f"user prompt {marker}\nTRAE\n一些回复但没有完成证据"
    reader = TraeReader(
        trae_cn_profile(),
        {"trae_poll_seconds": 0.01, "trae_stable_seconds": 0.01},
        state_reader=lambda: state,
        text_reader=lambda: {"ok": True, "text": text},
    )
    import pytest
    with pytest.raises(GuiAutomationError):
        reader.wait_reply(marker, started_at=time.time(), timeout=0.3,
                          stall_seconds=5)


def test_trae_reader_fallback_disabled_when_state_available(tmp_path):
    from agentloop_hub.gui.base import GuiAutomationError
    marker = "AGENTMESH_GUI_MARKER_WORKING"
    state = _hollow_trae_state(tmp_path, messages=2, status="working")
    result_file = tmp_path / "plan.json"
    result_file.write_text('{"plan_version": 1}', encoding="utf-8")
    text = f"user prompt {marker}\nTRAE 思考用时 1m\nreply"
    reader = TraeReader(
        trae_cn_profile(),
        {"trae_poll_seconds": 0.01, "trae_stable_seconds": 0.01},
        state_reader=lambda: state,
        text_reader=lambda: {"ok": True, "text": text},
    )
    reader.completion_path = str(result_file)
    import pytest
    with pytest.raises(GuiAutomationError):
        reader.wait_reply(marker, started_at=time.time(), timeout=0.3,
                          stall_seconds=5)


def test_send_trae_prompt_reports_ok(monkeypatch):
    """The receipt gate reads result['ok']; a successful click must set it."""
    import agentloop_hub.gui.cdp as cdp_mod
    monkeypatch.setattr(trae, "dismiss_trae_trust_dialog",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(trae, "pick_trae_page",
                        lambda *a, **k: {"id": "P1", "webSocketDebuggerUrl": "ws://x"})
    monkeypatch.setattr(cdp_mod, "send_prompt_via_cdp", lambda *a, **k: {"ok": True})

    class FakeTransport:
        def __init__(self, *a, **k):
            pass

        def close(self):
            pass
    monkeypatch.setattr(cdp_mod, "WebSocketTransport", FakeTransport)
    monkeypatch.setattr(cdp_mod, "_evaluate",
                        lambda *a, **k: {"ok": True, "method": "class-click"})
    result = trae.send_trae_prompt("hello", submit_retries=1)
    assert result["ok"] is True
    assert result["page_id"] == "P1"
    assert result["submit"]["ok"] is True
