"""Hard stop: cancel nodes, abort readers, no retry/resume."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from hub_test_support import make_project

from agentloop_hub.gui.base import GuiAutomationError
from agentloop_hub.gui.readers import WorkBuddyReader


def test_cancel_run_cancels_tasks_and_blocks_resume(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    imported = hub.import_plan(
        {"plan_version": 1, "goal": "cancel me", "constraints": [],
         "tasks": [{"task_id": "T1", "title": "scan", "type": "analysis",
                    "objective": "scan", "suggested_agent": "mock_worker",
                    "scope": [], "dependencies": []}]},
        main_agent_id="mock_main", model="mock-model",
    )
    for approval in hub.approvals_pending():
        if approval["kind"] == "architecture":
            hub.decide_approval(approval["approval_id"], approve=True)
    run_id = hub.approve_plan(imported["plan_id"])["run_id"]
    out = hub.cancel_run(run_id, reason="test stop")
    assert out["status"] == "canceled"
    assert "T1" in out["tasks"]
    run = hub.run_status(run_id)["run"]
    assert run["status"] == "canceled"
    task = hub.tasks(run_id=run_id)[0]
    assert task["status"] == "canceled"
    with pytest.raises(Exception):
        hub.resume_run(run_id, background=True)
    assert hub.start_run(run_id)["status"] == "canceled"


def test_workbuddy_reader_aborts_on_cancel_event(tmp_path):
    from test_workbuddy_completion import _make_db, _make_transcript, _profile

    marker = "MARKER_CANCEL"
    _make_transcript(tmp_path, marker)
    _make_db(tmp_path, "working")
    reader = WorkBuddyReader(
        _profile(tmp_path),
        {"session_db": str(tmp_path / "workbuddy.db"),
         "poll_seconds": 0.05, "quiet_seconds": 0.1, "send_timeout_seconds": 5},
    )
    reader.completion_path = str(tmp_path / "result.json")
    event = threading.Event()
    reader.cancel_event = event

    def stop():
        time.sleep(0.3)
        event.set()

    thread = threading.Thread(target=stop)
    thread.start()
    try:
        with pytest.raises(GuiAutomationError) as info:
            reader.wait_reply(marker, started_at=time.time() - 2, timeout=5,
                              stall_seconds=60)
        assert info.value.code == "GUI_CANCELED"
    finally:
        thread.join(timeout=2)
