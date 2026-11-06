"""Submission receipts: unknown acceptance must stop, not resend."""

from __future__ import annotations

from pathlib import Path

import pytest

from hub_test_support import default_plan, make_project

from agentloop_hub.adapters.base import ExecutionOutcome


class _FakeAdapter:
    def __init__(self, outcome: ExecutionOutcome):
        self.outcome = outcome
        self.reconciled: list[str] = []

    def execute(self, request):
        return self.outcome

    def reconcile_submission(self, attempt):
        self.reconciled.append(attempt["attempt_id"])
        return {"status": "accepted", "marker": attempt["binding"]["marker"],
                "reason": "marker_in_transcript",
                "reconciled_at": "2026-11-06T00:00:00Z"}


def _run_with(monkeypatch, tmp_path: Path, outcome: ExecutionOutcome):
    from agentloop_hub.control import runner as runner_module

    repo, hub, _ = make_project(tmp_path)
    imported = hub.import_plan(default_plan(str(repo)), main_agent_id="mock_main",
                               model="mock-model")
    approved = hub.decide_approval(imported["approval_id"], approve=True)
    run_id = approved["outcome"]["run_id"]
    fake = _FakeAdapter(outcome)
    monkeypatch.setattr(runner_module, "create_adapter", lambda agent: fake)
    hub.start_run(run_id, max_parallel=1, background=False)
    task = hub.store.list_tasks(run_id)[0]
    return repo, hub, task, fake


@pytest.mark.parametrize("status", ["accepted", "acceptance_unknown"])
def test_unconfirmed_or_accepted_submission_is_not_retried(
        monkeypatch, tmp_path: Path, status: str):
    outcome = ExecutionOutcome(
        status="timeout", error="GUI_TIMEOUT: no reply", error_code="GUI_TIMEOUT",
        submission_status=status,
        submission_receipt={"status": status, "marker": "M1"},
    )
    _, hub, task, _ = _run_with(monkeypatch, tmp_path, outcome)
    assert task.status == "waiting_user", task
    assert task.error.startswith("SUBMISSION_")
    assert task.attempts == 1
    assert len(hub.store.list_attempts(task.uid)) == 1
    assert hub.store.get_run(task.run_id)["status"] == "waiting_user"
    hub.close()


def test_reconcile_reads_the_original_request_without_a_new_attempt(
        monkeypatch, tmp_path: Path):
    outcome = ExecutionOutcome(
        status="timeout", error="GUI_TIMEOUT", error_code="GUI_TIMEOUT",
        submission_status="acceptance_unknown",
        submission_receipt={"status": "acceptance_unknown", "marker": "M1"},
    )
    _, hub, task, fake = _run_with(monkeypatch, tmp_path, outcome)
    result = hub.reconcile_task(task.uid)
    assert result["status"] == "accepted"
    assert result["verified"] is False
    assert fake.reconciled == [task.active_attempt_id]
    assert len(hub.store.list_attempts(task.uid)) == 1
    row = hub.store.get_task(task.uid)
    assert row.status == "waiting_user"
    assert "已确认送达" in (row.error or "")
    attempt = hub.store.get_attempt(task.active_attempt_id)
    assert attempt["binding"]["submission"]["status"] == "accepted"
    hub.close()


def test_resume_reconciles_submission_instead_of_requeueing(
        monkeypatch, tmp_path: Path):
    outcome = ExecutionOutcome(
        status="timeout", error="GUI_TIMEOUT", error_code="GUI_TIMEOUT",
        submission_status="acceptance_unknown",
        submission_receipt={"status": "acceptance_unknown", "marker": "M1"},
    )
    _, hub, task, fake = _run_with(monkeypatch, tmp_path, outcome)
    hub.resume_run(task.run_id, max_parallel=1, background=False)
    assert fake.reconciled
    assert len(hub.store.list_attempts(task.uid)) == 1
    hub.close()


def test_submit_retry_checks_marker_before_sending_again(tmp_path, monkeypatch):
    from agentloop_hub.adapters import gui as gui_module
    from agentloop_hub.gui.base import GuiProfile
    from test_gui_adapter import make_agent, make_request

    calls: list[str] = []

    class MarkerReader:
        def has_marker(self, marker, *, started_at=0):
            return {"ok": True, "path": "transcript.jsonl"}

    from agentloop_hub.gui import cdp as cdp_module
    monkeypatch.setattr(cdp_module, "submit_composer_via_cdp",
                        lambda **k: calls.append("deeplink") or {"ok": True})
    monkeypatch.setattr(cdp_module, "send_visible_prompt_via_cdp",
                        lambda *a, **k: calls.append("visible") or {"ok": True})
    adapter = gui_module.GuiAutomationAdapter(
        make_agent("workbuddy"),
        config={"mode": "gui_automation", "gui_app": "workbuddy"},
    )
    profile = GuiProfile(app="workbuddy", product="WorkBuddy",
                         window_title="WorkBuddy",
                         executable_candidates=(), transcript_reader="workbuddy")
    result, manual = adapter._submit_prompt(
        make_request(tmp_path), {"method": "workbuddy_deeplink",
                                 "workspace": str(tmp_path)},
        "MARKER", "prompt", tmp_path / "gui.log", profile,
        reader=MarkerReader(), started_at=1.0,
    )
    assert result.get("already_delivered") is True
    assert manual is None
    assert calls == []


def test_reconcile_passes_a_payload_dict_to_finalize(monkeypatch, tmp_path):
    outcome = ExecutionOutcome(
        status="timeout", error="GUI_TIMEOUT", error_code="GUI_TIMEOUT",
        submission_status="acceptance_unknown",
        submission_receipt={"status": "acceptance_unknown", "marker": "M1"},
    )
    repo, hub, task, fake = _run_with(monkeypatch, tmp_path, outcome)
    package = {
        "task_id": task.task_id, "agent_id": "mock_worker", "status": "completed",
        "summary": "done", "findings": [], "files_read": [], "files_changed": [],
        "commands_run": [], "tool_calls": [], "tests": [],
        "evidence": [{"file": "README.md", "lines": "1"}], "risks": [],
        "unresolved_items": [], "artifacts": [], "token_usage": {},
        "cost_estimate": {}, "recommended_next_action": [],
    }
    monkeypatch.setattr(fake, "reconcile_submission",
                        lambda attempt: {"status": "accepted", "package": package,
                                         "marker": "M1"})
    captured = {}
    monkeypatch.setattr(
        hub.runner, "_finalize_from_result",
        lambda task, agent, payload: captured.update(payload=payload))
    result = hub.reconcile_task(task.uid)
    assert result["verified"] is True
    assert isinstance(captured["payload"], dict)
    assert captured["payload"]["summary"] == "done"
    hub.close()
