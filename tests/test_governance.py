from __future__ import annotations

import pytest

from agentloop_hub.errors import ApprovalRequiredError
from conftest import make_project


def architecture_plan() -> dict:
    return {
        "plan_version": 1, "goal": "design storage layer",
        "constraints": ["keep local-first"],
        "tasks": [
            {
                "task_id": "ARCH", "title": "架构设计", "type": "architecture",
                "objective": "设计本地存储分层与接口边界",
                "suggested_agent": "mock_worker", "dependencies": [],
                "scope": [], "out_of_scope": [], "expected_outputs": ["design"],
                "acceptance_criteria": ["boundaries defined"],
                "verification_commands": [], "token_budget": 2000,
                "time_limit_minutes": 5, "retry_limit": 0, "risk_level": "low",
                "priority": 9,
            },
            {
                "task_id": "SCAN", "title": "扫描现状", "type": "analysis",
                "objective": "read calc.py and report",
                "suggested_agent": "mock_verifier", "dependencies": [],
                "scope": [], "out_of_scope": [], "expected_outputs": ["summary"],
                "acceptance_criteria": ["report"],
                "verification_commands": [], "token_budget": 2000,
                "time_limit_minutes": 5, "retry_limit": 0, "risk_level": "low",
                "priority": 3,
            },
        ],
    }


def test_architecture_gate_blocks_plan_until_approved(tmp_path):
    repo, hub, scenario_path = make_project(tmp_path)
    result = hub.import_plan(architecture_plan())
    assert result["architecture_approval_id"]
    with pytest.raises(ApprovalRequiredError):
        hub.decide_approval(result["approval_id"], approve=True)
    # Architecture approval first, then plan approval.
    arch = hub.decide_approval(result["architecture_approval_id"], approve=True)
    assert arch["outcome"]["status"] == "approved"
    approved = hub.decide_approval(result["approval_id"], approve=True)
    assert approved["outcome"]["status"] == "approved"
    run_id = approved["outcome"]["run_id"]
    outcome = hub.start_run(run_id, max_parallel=2)
    assert outcome["status"] == "pending_acceptance", outcome
    acceptance = hub.decide_approval(outcome["approval_id"], approve=True)
    assert acceptance["outcome"]["status"] == "pending_merge"
    merge_id = acceptance["outcome"]["approval_id"]
    merged = hub.decide_approval(merge_id, approve=True)["outcome"]
    assert merged["status"] == "completed", merged
    release = hub.request_release(run_id)
    assert release["status"] == "pending_approval" and release["approval_id"]
    released = hub.decide_approval(release["approval_id"], approve=True)["outcome"]
    assert released["status"] == "released", released
    status = hub.governance_status(run_id)
    assert status["acceptance_status"] == "approved"
    assert status["release_status"] == "approved"
    hub.close()


def test_acceptance_rejection_fails_run(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    from conftest import default_plan
    result = hub.import_plan(default_plan(str(tmp_path)))
    hub.decide_approval(result["approval_id"], approve=True)
    run_id = hub.latest_run()["run_id"]
    outcome = hub.start_run(run_id, max_parallel=2)
    rejected = hub.decide_approval(outcome["approval_id"], approve=False)["outcome"]
    assert rejected["status"] == "rejected"
    assert hub.store.get_run(run_id)["status"] == "failed"
    hub.close()


def test_release_requires_completed_and_acceptance(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    from conftest import default_plan
    result = hub.import_plan(default_plan(str(tmp_path)))
    hub.decide_approval(result["approval_id"], approve=True)
    run_id = hub.latest_run()["run_id"]
    with pytest.raises(Exception):
        hub.request_release(run_id)
    hub.start_run(run_id, max_parallel=2)
    with pytest.raises(Exception):
        hub.request_release(run_id)
    hub.close()
