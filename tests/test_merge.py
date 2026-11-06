from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentloop_hub.errors import ApprovalRequiredError, ConflictError
from agentloop_hub.models import TaskStatus
from hub_test_support import default_plan, make_project


def approve_and_run(hub, tmp_path):
    plan = default_plan(str(tmp_path))
    result = hub.import_plan(plan)
    hub.decide_approval(result["approval_id"], approve=True)
    run_id = hub.latest_run()["run_id"]
    outcome = hub.start_run(run_id, max_parallel=2)
    assert outcome["status"] == "pending_acceptance", outcome
    accepted = hub.decide_approval(outcome["approval_id"], approve=True)
    assert accepted["outcome"]["status"] == "pending_merge", accepted
    return run_id, {"approval_id": accepted["outcome"]["approval_id"],
                    "status": "pending_merge"}


def approve_merge(hub, approval_id):
    hub.approvals.decide(approval_id, approved=True)


def test_merge_requires_approval_then_applies(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    run_id, outcome = approve_and_run(hub, tmp_path)
    with pytest.raises(ApprovalRequiredError):
        hub.merge(run_id)
    approve_merge(hub, outcome["approval_id"])
    merged = hub.merge(run_id, approval_id=outcome["approval_id"])
    assert merged["status"] == "completed", merged
    assert "return a + b" in (repo / "calc.py").read_text(encoding="utf-8")
    run = hub.run_status(run_id)["run"]
    assert run["status"] == "completed"
    hub.close()


def test_merge_rewrites_final_summary(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    run_id, outcome = approve_and_run(hub, tmp_path)
    hub.store.update_run(run_id, summary={
        "summary_version": 1, "completion": "completed_with_warnings",
        "changes": [], "verification": "pre-merge", "risks": ["主工作区尚未应用补丁"],
        "merge_recommendation": "approve", "next_actions": [],
    })
    approve_merge(hub, outcome["approval_id"])
    merged = hub.merge(run_id, approval_id=outcome["approval_id"])
    assert merged["status"] == "completed"
    run = hub.run_status(run_id)["run"]
    summary = run["summary"]
    assert summary["final_status"] == "completed"
    assert summary["completion"] == "completed"
    assert summary["merge"]["status"] == "completed"
    assert summary["merge"]["applied_tasks"]
    assert "主工作区尚未应用补丁" not in summary["risks"]
    assert "合并完成" in summary["verification"]
    assert run["status"] == "completed"
    hub.close()


def test_merge_refuses_overlapping_user_change(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    run_id, outcome = approve_and_run(hub, tmp_path)
    # User edits the same file after the run started; merge must refuse.
    (repo / "calc.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    approve_merge(hub, outcome["approval_id"])
    with pytest.raises(ConflictError):
        hub.merge(run_id, approval_id=outcome["approval_id"])
    hub.close()


def test_rollback_file_after_merge(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    run_id, outcome = approve_and_run(hub, tmp_path)
    approve_merge(hub, outcome["approval_id"])
    hub.merge(run_id, approval_id=outcome["approval_id"])
    t3 = next(t for t in hub.tasks(run_id=run_id) if t["task_id"] == "T003")
    result = hub.rollback_file(t3["uid"], "calc.py")
    assert result["status"] == "reverted"
    assert "return a - b" in (repo / "calc.py").read_text(encoding="utf-8")
    hub.close()


def test_cross_task_same_file_conflict_detected(tmp_path):
    plan = default_plan(str(tmp_path))
    plan["tasks"] = [
        {"task_id": "A", "title": "a", "type": "implementation", "objective": "modify",
         "suggested_agent": "mock_worker", "dependencies": [], "scope": ["calc.py"],
         "out_of_scope": [], "expected_outputs": [], "acceptance_criteria": [],
         "verification_commands": [], "token_budget": 1000, "time_limit_minutes": 5,
         "retry_limit": 1, "risk_level": "low"},
        {"task_id": "B", "title": "b", "type": "implementation", "objective": "modify again",
         "suggested_agent": "mock_worker", "dependencies": [], "scope": ["calc.py"],
         "out_of_scope": [], "expected_outputs": [], "acceptance_criteria": [],
         "verification_commands": [], "token_budget": 1000, "time_limit_minutes": 5,
         "retry_limit": 1, "risk_level": "low"},
    ]
    scenario = {
        "tasks": {
            "A": {"write_files": [{"path": "calc.py",
                                   "content": "def add(a, b):\n    return a + b\n"}],
                  "summary": "a", "evidence": [{"file": "calc.py", "lines": "1-2"}]},
            "B": {"write_files": [{"path": "calc.py",
                                   "content": "def add(a, b):\n    return b + a\n"}],
                  "summary": "b", "evidence": [{"file": "calc.py", "lines": "1-2"}]},
        },
        "verification": {"checks": {"scope_check": "passed"}},
        "summary": {"summary_version": 1, "completion": "x", "changes": ["calc.py"],
                    "verification": "passed", "risks": [],
                    "merge_recommendation": "approve", "next_actions": []},
    }
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    result = hub.import_plan(plan)
    hub.decide_approval(result["approval_id"], approve=True)
    run_id = hub.latest_run()["run_id"]
    outcome = hub.start_run(run_id, max_parallel=2)
    assert outcome["status"] == "pending_acceptance", outcome
    accepted = hub.decide_approval(outcome["approval_id"], approve=True)
    merge_approval = accepted["outcome"]["approval_id"]
    approve_merge(hub, merge_approval)
    with pytest.raises(ConflictError):
        hub.merge(run_id, approval_id=merge_approval)
    hub.close()


def test_dirty_unrelated_file_does_not_block_merge(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    run_id, outcome = approve_and_run(hub, tmp_path)
    (repo / "unrelated.txt").write_text("user note\n", encoding="utf-8")
    approve_merge(hub, outcome["approval_id"])
    merged = hub.merge(run_id, approval_id=outcome["approval_id"])
    assert merged["status"] == "completed"
    assert (repo / "unrelated.txt").exists()
    hub.close()


def test_continuation_overlap_allows_dependency_chains_only():
    from agentloop_hub.control.merge import MergeService

    deps = {"T1": [], "T2": ["T1"], "R2_T2": ["T2"], "X1": [], "X2": []}
    assert MergeService._continuation_overlap(deps, "R2_T2", "T2")
    assert MergeService._continuation_overlap(deps, "T2", "T1")
    assert not MergeService._continuation_overlap(deps, "X1", "X2")
