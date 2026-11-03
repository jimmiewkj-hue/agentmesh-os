from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agentloop_hub.errors import ConflictError, ValidationError
from agentloop_hub.models import TaskStatus
from conftest import default_plan, make_project, register_mock_agents, write_scenario


def run_plan(hub, goal="fix add"):
    result = hub.request_plan(goal, main_agent_id="mock_main", model="mock-model")
    assert result["status"] == "pending_approval", result
    approved = hub.decide_approval(result["approval_id"], approve=True)
    return approved["outcome"]["run_id"]


def test_plan_approve_run_summary_merge_and_rollback(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    run_id = run_plan(hub)
    result = hub.start_run(run_id, max_parallel=2)
    assert result["status"] == "pending_acceptance", result
    status = hub.run_status(run_id)
    assert {t["task_id"]: t["status"] for t in status["tasks"]} == {
        "T001": "completed", "T002": "completed", "T003": "completed",
    }
    t3 = next(t for t in status["tasks"] if t["task_id"] == "T003")
    assert t3["verification_status"] in ("warning", "passed")
    assert t3["patch_path"] and Path(t3["patch_path"]).exists()
    # Independent verifier must not be the executor.
    verifications = hub.store.list_verifications(t3["uid"])
    assert verifications
    combined = verifications[-1]
    assert "control-plane" in combined["verifier_agent"]
    assert t3["assigned_agent"] != combined["verifier_agent"].replace("control-plane+", "")
    # Acceptance confirmation, then merge approval.
    accepted = hub.decide_approval(result["approval_id"], approve=True)
    assert accepted["outcome"]["status"] == "pending_merge", accepted
    merge_approval = accepted["outcome"]["approval_id"]
    outcome = hub.decide_approval(merge_approval, approve=True)["outcome"]
    assert outcome["status"] == "completed", outcome
    assert "return a + b" in (repo / "calc.py").read_text(encoding="utf-8")
    # Permissions/audit/cost.
    assert hub.audit_verify()["ok"]
    costs = hub.costs(run_id=run_id)
    assert costs["totals"]["total_tokens"] > 0
    # Rollback restores the original file.
    rollback = hub.rollback_task(t3["uid"])
    assert rollback["status"] == "reverted"
    assert "return a - b" in (repo / "calc.py").read_text(encoding="utf-8")
    hub.close()


def test_parallel_tasks_overlap_in_time(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    run_id = run_plan(hub)
    started = time.monotonic()
    result = hub.start_run(run_id, max_parallel=2)
    elapsed = time.monotonic() - started
    assert result["status"] == "pending_acceptance"
    tasks = {t["task_id"]: t for t in hub.tasks(run_id=run_id)}
    t1, t2 = tasks["T001"], tasks["T002"]
    assert t1["started_at"] and t2["started_at"]
    # Mock tasks sleep 0.4s each; with max_parallel=2 the two roots start
    # within a couple of seconds and the dependent task waits for T001.
    assert abs(
        __import__("datetime").datetime.fromisoformat(t1["started_at"].replace("Z", "+00:00")).timestamp()
        - __import__("datetime").datetime.fromisoformat(t2["started_at"].replace("Z", "+00:00")).timestamp()
    ) < 1.5
    assert tasks["T003"]["started_at"] >= t1["finished_at"]
    hub.close()


def test_retry_once_then_complete(tmp_path):
    scenario = {
        "tasks": {
            "T001": {"summary": "ok"},
            "T002": {"summary": "ok"},
            "T003": {"fail_once_marker": ".failed-once",
                     "write_files": [{"path": "calc.py",
                                      "content": "def add(a, b):\n    return a + b\n"}],
                     "summary": "fixed", "evidence": [{"file": "calc.py", "lines": "1-2"}]},
        },
        "verification": {"checks": {"scope_check": "passed"}},
        "summary": {"summary_version": 1, "completion": "done", "changes": ["calc.py"],
                    "verification": "passed", "risks": [],
                    "merge_recommendation": "approve", "next_actions": []},
    }
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    run_id = run_plan(hub)
    result = hub.start_run(run_id, max_parallel=2)
    assert result["status"] == "pending_acceptance", result
    t3 = next(t for t in hub.tasks(run_id=run_id) if t["task_id"] == "T003")
    assert t3["attempts"] >= 2
    assert t3["status"] == "completed"
    hub.close()


def test_scope_violation_fails_task(tmp_path):
    scenario = {
        "tasks": {
            "T001": {"summary": "ok"},
            "T002": {"summary": "ok"},
            "T003": {"write_files": [{"path": "README.md", "content": "tampered\n"}],
                     "summary": "bad"},
        },
        "verification": {"checks": {"scope_check": "passed"}},
        "summary": {"summary_version": 1, "completion": "x", "changes": ["README.md"],
                    "verification": "failed", "risks": [],
                    "merge_recommendation": "reject", "next_actions": []},
    }
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    run_id = run_plan(hub)
    result = hub.start_run(run_id, max_parallel=2)
    assert result["status"] == "failed"
    t3 = next(t for t in hub.tasks(run_id=run_id) if t["task_id"] == "T003")
    assert t3["status"] == "failed"
    assert "超出" in t3["error"] or "范围" in t3["error"] or "验证" in t3["error"]
    hub.close()


def test_high_risk_task_waits_for_approval_then_resumes(tmp_path):
    plan = default_plan(str(tmp_path))
    plan["tasks"][2]["scope"] = ["package-lock.json"]
    plan["tasks"][2]["verification_commands"] = []
    scenario = {
        "plan": plan,
        "tasks": {
            "T001": {"summary": "ok"},
            "T002": {"summary": "ok"},
            "T003": {"write_files": [{"path": "package-lock.json", "content": "{}\n"}],
                     "summary": "lock updated",
                     "evidence": [{"file": "package-lock.json", "lines": "1",
                                   "description": "lock file updated"}]},
        },
        "verification": {"checks": {"scope_check": "passed"}},
        "summary": {"summary_version": 1, "completion": "done", "changes": [],
                    "verification": "passed", "risks": [],
                    "merge_recommendation": "approve", "next_actions": []},
    }
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    run_id = run_plan(hub)
    result = hub.start_run(run_id, max_parallel=2)
    assert result["status"] == "waiting_user"
    pending = hub.approvals_pending()
    high = [a for a in pending if a["kind"] == "high_risk" and a["task_uid"].endswith(":T003")]
    assert high
    hub.decide_approval(high[0]["approval_id"], approve=True)
    result2 = hub.resume_run(run_id, max_parallel=2)
    assert result2["status"] == "pending_acceptance"
    hub.close()


def test_analysis_result_cache_reuse(tmp_path):
    plan = {
        "plan_version": 1, "goal": "scan only", "constraints": [],
        "tasks": [{
            "task_id": "A1", "title": "scan calc", "type": "analysis",
            "objective": "read calc.py and report", "suggested_agent": "mock_worker",
            "dependencies": [], "scope": ["calc.py"], "out_of_scope": [],
            "expected_outputs": ["summary"], "acceptance_criteria": ["report"],
            "verification_commands": [], "token_budget": 5000,
            "time_limit_minutes": 5, "retry_limit": 0, "risk_level": "low",
        }],
    }
    scenario = {"plan": plan, "tasks": {"A1": {
        "summary": "scan done", "findings": [{"severity": "low", "summary": "ok"}],
        "evidence": [{"file": "calc.py", "lines": "1-2", "description": "read"}],
    }}, "summary": {"summary_version": 1, "completion": "scan done", "changes": [],
                    "verification": "passed", "risks": [],
                    "merge_recommendation": "approve", "next_actions": []}}
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    run_ids = []
    for _ in range(2):
        result = hub.import_plan(plan)
        hub.decide_approval(result["approval_id"], approve=True)
        run_id = hub.latest_run()["run_id"]
        outcome = hub.start_run(run_id, max_parallel=1)
        assert outcome["status"] == "pending_acceptance", outcome
        run_ids.append(run_id)
    assert any(e["action"] == "task.cache_hit" for e in hub.audit_events())
    second_task = next(t for t in hub.tasks(run_id=run_ids[1]) if t["task_id"] == "A1")
    assert second_task["status"] == "completed"
    hub.close()


def test_failure_circuit_breaker_pauses_remaining(tmp_path):
    plan = default_plan(str(tmp_path))
    plan["tasks"] = [
        {"task_id": f"F{i}", "title": "fail", "type": "analysis", "objective": "fail",
         "suggested_agent": "mock_worker", "dependencies": [], "scope": [],
         "out_of_scope": [], "expected_outputs": [], "acceptance_criteria": [],
         "verification_commands": [], "token_budget": 1000, "time_limit_minutes": 5,
         "retry_limit": 0, "risk_level": "low"}
        for i in range(3)
    ]
    scenario = {"plan": plan, "tasks": {
        f"F{i}": {"summary": "failed", "result": {"status": "failed"}} for i in range(3)
    }}
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    hub.config.max_failures_before_circuit_break = 2
    run_id = run_plan(hub)
    result = hub.start_run(run_id, max_parallel=1)
    assert result["status"] == "paused", result
    tasks = {t["task_id"]: t for t in hub.tasks(run_id=run_id)}
    assert sum(1 for t in tasks.values() if t["status"] == "failed") >= 2
    assert any(t["status"] == "paused" for t in tasks.values())
    assert any(e["action"] == "run.circuit_breaker" for e in hub.audit_events())
    hub.close()


def test_budget_blocks_dispatch(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    # Two analysis tasks fit, but the 10k implementation cannot fit the 12k
    # project budget after the first tasks consume tokens.
    hub.set_project_budget(12000)
    run_id = run_plan(hub)
    result = hub.start_run(run_id, max_parallel=2)
    assert result["status"] == "waiting_user"
    pending = hub.approvals_pending()
    assert any(a["kind"] == "budget" for a in pending)
    t3 = next(t for t in hub.tasks(run_id=run_id) if t["task_id"] == "T003")
    assert t3["status"] == "waiting_user"
    budget_approval = next(a for a in pending if a["kind"] == "budget")
    hub.decide_approval(budget_approval["approval_id"], approve=True)
    resumed = hub.resume_run(run_id, max_parallel=2)
    assert resumed["status"] == "pending_acceptance", resumed
    hub.close()


def test_relay_result_submission_and_resume(tmp_path):
    """A GUI/file-relay agent stays waiting until a human writes the result."""
    repo, hub, scenario_path = make_project(tmp_path)
    # Replace the worker with a file-relay agent for T001 only.
    t1_agent = hub.register_agent({
        "agent_id": "relay_worker", "name": "Relay Worker", "adapter_type": "file_relay",
        "access_level": "L2",
        "adapter_config": {"session_reader": "", "wait_seconds": 0},
        "capabilities": {"read_files": True, "write_files": True, "terminal": True,
                         "analysis": True, "structured_output": False},
    })
    # Explicitly opt in to manual relay; automatic runs must not select it.
    hub.config.router_allow_manual_relay = True
    hub.router.config = hub.config
    plan = default_plan(str(tmp_path))
    plan["tasks"][0]["suggested_agent"] = "relay_worker"
    hub.import_plan(plan)
    # reload scenario with this plan for the main agent
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    scenario["plan"] = plan
    write_scenario(scenario_path, scenario)
    hub.decide_approval(hub.approvals_pending()[0]["approval_id"], approve=True)
    run_id = hub.latest_run()["run_id"]
    result = hub.start_run(run_id, max_parallel=2)
    assert result["status"] == "waiting_user", result
    t1 = next(t for t in hub.tasks(run_id=run_id) if t["task_id"] == "T001")
    assert t1["status"] == "waiting_user"
    relay = Path(hub.project.root) / ".agentloop" / "relay" / t1["uid"].replace(":", "_")
    assert (relay / "inbox" / "prompt.md").exists()
    payload = {
        "task_id": "T001", "agent_id": "relay_worker", "status": "completed",
        "summary": "relay done", "findings": [{"severity": "low", "summary": "x"}],
        "files_read": [], "files_changed": [], "commands_run": [], "tool_calls": [],
        "tests": [], "evidence": [{"file": "calc.py", "lines": "1-2", "description": "x"}],
        "risks": [], "unresolved_items": [], "artifacts": [],
        "token_usage": {"input": 10, "output": 2}, "cost_estimate": {},
        "recommended_next_action": [],
    }
    hub.submit_result(t1["uid"], payload)
    # After submitting the result the control plane can finish verification.
    result2 = hub.start_run(run_id, max_parallel=2)
    assert result2["status"] in ("pending_acceptance", "waiting_user"), result2
    hub.close()
