from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentloop_hub.errors import ValidationError
from conftest import default_plan, make_project, register_mock_agents


def test_request_plan_with_mock_main(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    result = hub.request_plan("fix add", main_agent_id="mock_main", model="mock-model")
    assert result["status"] == "pending_approval"
    plan = hub.plan(result["plan_id"])
    assert [t["task_id"] for t in plan["tasks"]] == ["T001", "T002", "T003"]
    approval = hub.approvals_pending()[0]
    assert approval["kind"] == "plan"
    hub.close()


def test_invalid_plan_fails_cleanly(tmp_path):
    scenario = {"plan": {
        "plan_version": 1, "goal": "bad", "tasks": [{
            "task_id": "T1", "title": "x", "objective": "y", "type": "implementation",
            "scope": [], "dependencies": [],
        }],
    }}
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    result = hub.request_plan("bad", main_agent_id="mock_main")
    assert result["status"] == "failed"
    assert "scope" in result["error"] or "校验" in result["error"]
    hub.close()


def test_import_plan_and_approve_creates_tasks(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    data = default_plan(str(tmp_path))
    result = hub.import_plan(data)
    assert result["status"] == "pending_approval"
    approved = hub.approve_plan(result["plan_id"], approval_id=result["approval_id"])
    assert approved["status"] == "approved"
    assert approved["tasks"] == 3
    run = hub.run_status(approved["run_id"])
    assert run["run"]["status"] == "ready"
    assert len(run["tasks"]) == 3
    assert all(t["status"] == "queued" for t in run["tasks"])
    hub.close()


def test_plan_repair_once_with_agent(tmp_path):
    invalid = {
        "plan_version": 1, "goal": "repair me", "tasks": [{
            "task_id": "T1", "title": "x", "objective": "y", "type": "implementation",
            "scope": [], "dependencies": [],
        }],
    }
    valid = {
        "plan_version": 1, "goal": "repair me", "tasks": [{
            "task_id": "T1", "title": "x", "objective": "y", "type": "analysis",
            "scope": [], "dependencies": [],
        }],
    }
    scenario = {"plan_sequence": [invalid, valid]}
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    result = hub.request_plan("repair me", main_agent_id="mock_main")
    assert result["status"] == "pending_approval", result
    assert hub.plan(result["plan_id"])["tasks"][0]["type"] == "analysis"
    assert any(e["action"] == "plan.repair" for e in hub.audit_events())
    hub.close()


def test_reject_plan(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    result = hub.import_plan(default_plan(str(tmp_path)))
    rejected = hub.reject_plan(result["plan_id"], approval_id=result["approval_id"], note="no")
    assert rejected["status"] == "rejected"
    assert hub.plan(result["plan_id"])["status"] == "rejected"
    hub.close()
