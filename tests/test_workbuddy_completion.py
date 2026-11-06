"""WorkBuddy must not finish just because result.json appeared early."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from agentloop_hub.gui.base import GuiAutomationError, GuiProfile
from agentloop_hub.gui.readers import WorkBuddyReader


def _profile(root: Path) -> GuiProfile:
    return GuiProfile(
        app="workbuddy", product="WorkBuddy", window_title="WorkBuddy",
        executable_candidates=(), transcript_reader="workbuddy",
        data_root=str(root),
    )


def _make_transcript(root: Path, marker: str) -> Path:
    project = root / "demo"
    project.mkdir(parents=True)
    path = project / "sess-1.jsonl"
    events = [
        {"type": "message", "role": "user", "sessionId": "sess-1",
         "content": f"task {marker}"},
        {"type": "message", "role": "assistant", "status": "completed",
         "sessionId": "sess-1",
         "providerData": {"model": "glm-5.3-flash"},
         "content": [{"type": "output_text", "text": "intermediate answer"}]},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def _make_db(root: Path, status: str) -> Path:
    db = root / "workbuddy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions("
        "id TEXT PRIMARY KEY, cwd TEXT, title TEXT, status TEXT, model TEXT,"
        "created_at INTEGER, updated_at INTEGER, last_activity_at INTEGER)"
    )
    now_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?,?,?)",
        ("sess-1", str(root), "demo", status, "glm-5.3-flash",
         now_ms, now_ms, now_ms),
    )
    conn.commit()
    conn.close()
    return db


def test_result_file_alone_does_not_finish_while_session_is_working(tmp_path):
    marker = "MARKER_EARLY_RESULT"
    _make_transcript(tmp_path, marker)
    _make_db(tmp_path, "working")
    (tmp_path / "result.json").write_text('{"task_id":"T1"}', encoding="utf-8")
    reader = WorkBuddyReader(
        _profile(tmp_path),
        {"session_db": str(tmp_path / "workbuddy.db"),
         "workbuddy_result_settle_seconds": 0.2, "poll_seconds": 0.05,
         "quiet_seconds": 0.1, "send_timeout_seconds": 5},
    )
    reader.completion_path = str(tmp_path / "result.json")

    with pytest.raises(GuiAutomationError) as info:
        reader.wait_reply(marker, started_at=time.time() - 2, timeout=0.8,
                          stall_seconds=60)
    assert info.value.code == "GUI_TIMEOUT"

    _make_db(tmp_path, "completed")
    reply = reader.wait_reply(marker, started_at=time.time() - 2, timeout=5,
                              stall_seconds=60)
    assert reply.session_id == "sess-1"
    assert reply.text


def test_adapter_blocks_when_workbuddy_still_has_active_task(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agentloop_hub.adapters import gui as gui_module
    from test_gui_adapter import make_agent, make_request

    class BusyReader:
        page_id = ""

        def active_sessions(self, **_kwargs):
            return [{"id": "sess-busy", "cwd": "x", "title": "running",
                     "status": "working", "last_activity_at": 1}]

    monkeypatch.setattr(gui_module, "create_reader", lambda *a, **k: BusyReader())
    adapter = gui_module.GuiAutomationAdapter(
        make_agent("workbuddy"),
        config={"mode": "gui_automation", "gui_app": "workbuddy",
                "workbuddy_active_wait_seconds": 0.1},
    )
    request = make_request(tmp_path)
    with pytest.raises(GuiAutomationError) as info:
        adapter._prepare_interface(
            SimpleNamespace(app="workbuddy"), request, "prompt", "marker",
            tmp_path / "gui.log", time.time() - 1,
        )
    assert info.value.code == "APP_BUSY"


def test_active_sessions_only_returns_running_rows(tmp_path):
    _make_db(tmp_path, "working")
    conn = sqlite3.connect(tmp_path / "workbuddy.db")
    now_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?,?,?)",
        ("sess-2", str(tmp_path), "done", "completed", "glm-5.3-flash",
         now_ms, now_ms, now_ms),
    )
    conn.commit()
    conn.close()
    reader = WorkBuddyReader(
        _profile(tmp_path), {"session_db": str(tmp_path / "workbuddy.db")})
    active = reader.active_sessions()
    assert [item["id"] for item in active] == ["sess-1"]


def test_working_session_is_not_a_stall(tmp_path, monkeypatch):
    marker = "MARKER_LONG_THINKING"
    _make_transcript(tmp_path, marker)
    _make_db(tmp_path, "working")
    reader = WorkBuddyReader(
        _profile(tmp_path),
        {"session_db": str(tmp_path / "workbuddy.db"), "poll_seconds": 0.05,
         "quiet_seconds": 0.1, "send_timeout_seconds": 5},
    )
    reader.completion_path = str(tmp_path / "result.json")
    monkeypatch.setattr(reader, "ui_busy", lambda: False)
    with pytest.raises(GuiAutomationError) as info:
        reader.wait_reply(marker, started_at=time.time() - 2, timeout=0.8,
                          stall_seconds=0.2)
    # The model may stream without transcript events; only the overall task
    # timeout may stop it, not the transcript stall timer.
    assert info.value.code == "GUI_TIMEOUT"


def test_incomplete_provider_message_surfaces_stream_error(tmp_path, monkeypatch):
    marker = "MARKER_STREAM_ERROR"
    transcript = _make_transcript(tmp_path, marker)
    _make_db(tmp_path, "working")
    reader = WorkBuddyReader(
        _profile(tmp_path),
        {"session_db": str(tmp_path / "workbuddy.db"), "poll_seconds": 0.05,
         "quiet_seconds": 0.1, "send_timeout_seconds": 5},
    )
    reader.completion_path = str(tmp_path / "result.json")
    monkeypatch.setattr(reader, "ui_busy", lambda: True)

    def append_error():
        time.sleep(0.3)
        error = {
            "type": "message", "role": "assistant", "status": "incomplete",
            "sessionId": "sess-1",
            "providerData": {"error": {
                "message": "Empty stream: upstream ended the turn with "
                           "finish_reason=error (elapsed=900152ms)",
                "isStreamTimeout": True,
            }},
            "content": [{"type": "output_text",
                         "text": "Model returned no valid response."}],
        }
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(error) + "\n")

    thread = threading.Thread(target=append_error)
    thread.start()
    with pytest.raises(GuiAutomationError) as info:
        reader.wait_reply(marker, started_at=time.time() - 2, timeout=3,
                          stall_seconds=10)
    thread.join(timeout=2)
    assert info.value.code == "MODEL_STREAM_ERROR"
    assert "Empty stream" in info.value.message


def test_adapter_ignores_stale_working_row_when_app_is_not_running(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from agentloop_hub.adapters import gui as gui_module
    from test_gui_adapter import make_agent, make_request

    class BusyReader:
        page_id = ""

        def active_sessions(self, **_kwargs):
            return [{"id": "sess-dead", "cwd": "x", "title": "running",
                     "status": "working", "last_activity_at": 1}]

    class CdpResult:
        ok = True

        def to_dict(self):
            return {"ok": True}

    monkeypatch.setattr(gui_module, "create_reader", lambda *a, **k: BusyReader())
    monkeypatch.setattr("agentloop_hub.control.apps.process_running",
                        lambda image: False)
    monkeypatch.setattr(gui_module, "ensure_workbuddy_cdp",
                        lambda **kwargs: CdpResult())
    adapter = gui_module.GuiAutomationAdapter(
        make_agent("workbuddy"),
        config={"mode": "gui_automation", "gui_app": "workbuddy",
                "executable": "C:/Program Files/WorkBuddy/WorkBuddy.exe",
                "workbuddy_active_wait_seconds": 5},
    )
    request = make_request(tmp_path)
    log_path = tmp_path / "gui.log"
    try:
        adapter._prepare_interface(
            SimpleNamespace(app="workbuddy"), request, "prompt", "marker",
            log_path, time.time() - 1,
        )
    except GuiAutomationError as exc:
        assert exc.code != "APP_BUSY"
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert any(e.get("event") == "workbuddy_stale_session_ignored" for e in events)
