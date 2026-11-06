"""S04: attempts, receipts and session binding are first-class execution identity."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hub_test_support import default_scenario, make_project

from agentloop_hub.adapters import gui as gui_module
from agentloop_hub.gui.base import GuiAutomationError


def _retry_scenario() -> dict:
    base = default_scenario()
    base["plan"] = {
        "plan_version": 1, "goal": "retry attempt", "constraints": [],
        "tasks": [{
            "task_id": "T1", "title": "fix", "type": "implementation",
            "objective": "fix calc", "suggested_agent": "mock_worker",
            "dependencies": [], "scope": ["calc.py"], "out_of_scope": [],
            "expected_outputs": ["code_patch"], "acceptance_criteria": ["tests pass"],
            "verification_commands": [], "token_budget": 5000,
            "time_limit_minutes": 5, "retry_limit": 1, "risk_level": "low",
        }],
    }
    base["tasks"] = {"T1": {
        "fail_once_marker": "t1.failed",
        "write_files": [{"path": "calc.py",
                         "content": "def add(a, b):\n    return a + b\n"}],
        "summary": "fixed", "tests": [{"command": "unit", "result": "passed"}],
        "evidence": [{"file": "calc.py", "lines": "1-2", "description": "fix"}],
    }}
    return base


def test_retry_creates_a_new_attempt_with_new_artifacts(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path, scenario=_retry_scenario())
    requested = hub.request_plan("retry attempt", main_agent_id="mock_main",
                                 model="mock-model")
    approved = hub.decide_approval(requested["approval_id"], approve=True)
    run_id = approved["outcome"]["run_id"]
    hub.start_run(run_id, max_parallel=1)
    task = next(t for t in hub.tasks(run_id=run_id) if t["task_id"] == "T1")
    assert task["status"] == "completed", task
    attempts = hub.store.list_attempts(task["uid"])
    assert len(attempts) == 2, attempts
    assert attempts[0]["request_id"] != attempts[1]["request_id"]
    assert attempts[0]["result_path"] != attempts[1]["result_path"]
    assert attempts[0]["attempt_number"] == 1
    assert attempts[1]["attempt_number"] == 2
    # The active attempt is the successful retry, and its patch is not the
    # first attempt's file.
    assert task["active_attempt_id"] == attempts[1]["attempt_id"]
    assert attempts[1]["patch_path"]
    assert attempts[0]["patch_path"] != attempts[1]["patch_path"]
    hub.close()


def test_resume_failure_blocks_instead_of_opening_a_new_session(
        tmp_path: Path, monkeypatch):
    from agentloop_hub.control.interfaces import InterfaceOutcome

    class FakeReader:
        def active_sessions(self, **_kwargs):
            return []

    monkeypatch.setattr(gui_module, "create_reader", lambda *a, **k: FakeReader())
    monkeypatch.setattr(
        gui_module, "ensure_workbuddy_cdp",
        lambda **k: InterfaceOutcome(True, "cdp", {"port": 9223}, {},
                                     evidence={"port": 9223}))
    monkeypatch.setattr(
        gui_module, "open_workbuddy_chat",
        lambda **k: InterfaceOutcome(True, "deeplink", {}, {},
                                     evidence={"uri": "workbuddy://chat"}))
    from agentloop_hub.gui import cdp as cdp_module
    monkeypatch.setattr(cdp_module, "wait_for_workbuddy_conversation",
                        lambda *a, **k: {"ok": False, "reason": "CONVERSATION_MISMATCH"})
    monkeypatch.setattr(cdp_module, "switch_workbuddy_conversation",
                        lambda *a, **k: {"ok": False, "reason": "EMPTY_CURRENT_ID"})
    called: list[str] = []
    monkeypatch.setattr(gui_module, "prepare_workbuddy_task",
                        lambda **k: called.append("prepared") or InterfaceOutcome(
                            True, "deeplink", {}, {}, evidence={"started_at": 1.0}))
    from test_gui_adapter import make_agent, make_request

    adapter = gui_module.GuiAutomationAdapter(
        make_agent("workbuddy"),
        config={"mode": "gui_automation", "gui_app": "workbuddy"},
    )
    request = make_request(tmp_path)
    request.model = "glm-5.3-flash"
    request.resume_session = {"resume": True, "session_id": "sess-1",
                              "workdir": str(tmp_path / "work")}
    with pytest.raises(GuiAutomationError) as info:
        adapter._prepare_interface(
            SimpleNamespace(app="workbuddy"), request, "prompt", "marker",
            tmp_path / "gui.log", time.time() - 1,
        )
    assert info.value.code == "SESSION_BINDING_UNCONFIRMED"
    assert called == []


def test_reverify_uses_the_active_attempt_artifact_not_task_latest(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path, scenario=_retry_scenario())
    requested = hub.request_plan("retry attempt", main_agent_id="mock_main",
                                 model="mock-model")
    approved = hub.decide_approval(requested["approval_id"], approve=True)
    run_id = approved["outcome"]["run_id"]
    hub.start_run(run_id, max_parallel=1)
    task = next(t for t in hub.tasks(run_id=run_id) if t["task_id"] == "T1")
    attempts = hub.store.list_attempts(task["uid"])
    assert len(attempts) == 2
    active = attempts[-1]
    # Poison the task-level "latest" result; reverify must ignore it.
    poison = Path(task["task_dir"]) / "result.json"
    if poison.exists():
        import json as _json
        payload = _json.loads(poison.read_text(encoding="utf-8"))
        payload["summary"] = "OLD TASK-LATEST RESULT"
        poison.write_text(_json.dumps(payload), encoding="utf-8")
    hub.store.set_task_status(task["uid"], "failed", force=True)
    out = hub.reverify_task(task["uid"])
    assert out["task_uid"] == task["uid"]
    stored = hub.store.get_result(task["uid"])
    assert stored["payload"]["summary"] != "OLD TASK-LATEST RESULT"
    events = [e for e in hub.audit_events() if e["action"] == "task.reverify_requested"]
    assert events and events[-1]["payload"]["attempt_id"] == active["attempt_id"]
    assert events[-1]["payload"]["legacy_unbound"] is False
    hub.close()


def _repair_fixture(monkeypatch, tmp_path, repaired_payload):
    from agentloop_hub.adapters.base import ExecutionOutcome
    from agentloop_hub.control import runner as runner_module
    from agentloop_hub.models import Task, TaskSpec

    repo, hub, _ = make_project(tmp_path)
    agent = hub.get_agent("mock_worker")
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    task = Task(
        uid="run_repair:T1", project_id=hub.project.project_id, plan_id="p",
        run_id="run_repair",
        spec=TaskSpec(task_id="T1", title="t", objective="scan", type="analysis"),
        status="running", assigned_agent="mock_worker", workdir=str(repo),
        task_dir=str(task_dir), base_commit=hub.project.commit,
        active_attempt_id="att-repair",
    )
    hub.store.create_attempt(
        attempt_id="att-repair", project_id=hub.project.project_id,
        task_uid=task.uid, run_id=task.run_id, attempt_number=1,
        request_id="req-repair", workdir=str(repo),
        result_path=str(tmp_path / "original.json"),
    )
    original_path = tmp_path / "original.json"
    original = {
        "task_id": "T1", "agent_id": "mock_worker", "status": "completed",
        "summary": "scan done", "findings": [], "files_read": ["README.md"],
        "files_changed": [], "commands_run": [], "tool_calls": [], "tests": [],
        "evidence": [], "risks": [], "unresolved_items": [], "artifacts": [],
        # token_usage/cost_estimate/recommended_next_action are missing: invalid
    }
    original_path.write_text(__import__("json").dumps(original), encoding="utf-8")

    class RepairAdapter:
        def execute(self, request):
            path = Path(request.result_path)
            path.write_text(__import__("json").dumps(repaired_payload),
                            encoding="utf-8")
            return ExecutionOutcome(status="completed", result_path=str(path),
                                    result_payload=repaired_payload)

    monkeypatch.setattr(runner_module, "create_adapter", lambda _agent: RepairAdapter())
    outcome = SimpleNamespace(result_path=str(original_path), status="failed",
                              error_code="GUI_RESULT_INVALID", usage=None)
    return hub, task, agent, outcome, original


def _valid_repaired(original: dict) -> dict:
    payload = dict(original)
    payload.update({"token_usage": {}, "cost_estimate": {},
                    "recommended_next_action": []})
    return payload


def test_format_repair_keeps_status_and_is_single_shot(monkeypatch, tmp_path):
    original = {
        "task_id": "T1", "agent_id": "mock_worker", "status": "completed",
        "summary": "scan done", "findings": [], "files_read": ["README.md"],
        "files_changed": [], "commands_run": [], "tool_calls": [], "tests": [],
        "evidence": [], "risks": [], "unresolved_items": [], "artifacts": [],
    }
    hub, task, agent, outcome, _ = _repair_fixture(
        monkeypatch, tmp_path, _valid_repaired(original))
    result = hub.runner._repair_result_package(
        task, agent, "att-repair", "req-repair", outcome)
    assert result is not None
    assert result.status == "completed"
    attempt = hub.store.get_attempt("att-repair")
    assert attempt["binding"]["result_repair"]["status"] == "applied"
    # A second repair is never requested.
    again = hub.runner._repair_result_package(
        task, agent, "att-repair", "req-repair", outcome)
    assert again is None
    hub.close()


def test_format_repair_cannot_add_tests_or_evidence(monkeypatch, tmp_path):
    original = {
        "task_id": "T1", "agent_id": "mock_worker", "status": "completed",
        "summary": "scan done", "findings": [], "files_read": ["README.md"],
        "files_changed": [], "commands_run": [], "tool_calls": [], "tests": [],
        "evidence": [], "risks": [], "unresolved_items": [], "artifacts": [],
    }
    fabricated = _valid_repaired(original)
    fabricated["tests"] = [{"command": "pytest", "result": "passed"}]
    fabricated["evidence"] = [{"file": "calc.py", "lines": "1"}]
    hub, task, agent, outcome, _ = _repair_fixture(monkeypatch, tmp_path, fabricated)
    result = hub.runner._repair_result_package(
        task, agent, "att-repair", "req-repair", outcome)
    assert result is None
    attempt = hub.store.get_attempt("att-repair")
    assert attempt["binding"]["result_repair"]["status"] == "failed"
    assert "补造" in attempt["binding"]["result_repair"]["error"]
    hub.close()


def test_manual_submission_requires_matching_attempt_identity(tmp_path: Path):
    import pytest as _pytest
    from agentloop_hub.errors import ConflictError, ValidationError

    repo, hub, _ = make_project(tmp_path, scenario=_retry_scenario())
    requested = hub.request_plan("retry attempt", main_agent_id="mock_main",
                                 model="mock-model")
    approved = hub.decide_approval(requested["approval_id"], approve=True)
    run_id = approved["outcome"]["run_id"]
    hub.start_run(run_id, max_parallel=1)
    task = next(t for t in hub.tasks(run_id=run_id) if t["task_id"] == "T1")
    payload = hub.store.get_result(task["uid"])["payload"]
    with _pytest.raises(ValidationError):
        hub.submit_result(task["uid"], payload)
    with _pytest.raises(ConflictError):
        hub.submit_result(task["uid"], payload, attempt_id="att-wrong")
    with _pytest.raises(ConflictError):
        hub.submit_result(task["uid"], payload, request_id="req-wrong")
    attempt = hub.store.get_attempt(task["active_attempt_id"])
    out = hub.submit_result(task["uid"], payload,
                            attempt_id=task["active_attempt_id"],
                            request_id=attempt["request_id"])
    assert out["task_uid"] == task["uid"]
    hub.close()
