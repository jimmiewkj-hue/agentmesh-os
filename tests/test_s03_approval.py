"""S03: approvals are bound to an immutable plan revision and create runs atomically."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hub_test_support import default_plan, make_project

from agentloop_hub.errors import ConflictError


def _import(hub, repo: Path, goal: str = "gate"):
    payload = default_plan(str(repo))
    payload["goal"] = goal
    return hub.import_plan(payload, main_agent_id="mock_main", model="mock-model")


def _settle_architecture(hub, approval_id: str) -> None:
    for approval in hub.approvals_pending():
        if approval["kind"] == "architecture" and approval.get("plan_id"):
            hub.decide_approval(approval["approval_id"], approve=True)


def test_cross_plan_and_wrong_kind_tickets_are_rejected(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path)
    plan_a = _import(hub, repo, "plan A")
    plan_b = _import(hub, repo, "plan B")
    _settle_architecture(hub, plan_a["approval_id"])
    _settle_architecture(hub, plan_b["approval_id"])

    with pytest.raises(ConflictError):
        hub.planning.approve_plan(plan_a["plan_id"],
                                  approval_id=plan_b["approval_id"])

    # A non-plan ticket cannot approve a plan either.
    other = hub.approvals.create("merge", "not a plan", {}, run_id="run-x")
    with pytest.raises(ConflictError):
        hub.planning.approve_plan(plan_a["plan_id"], approval_id=other.approval_id)

    assert hub.store.get_plan(plan_a["plan_id"]).status == "pending_approval"
    assert hub.store.get_plan(plan_b["plan_id"]).status == "pending_approval"
    hub.close()


def test_editing_a_plan_expires_the_old_ticket_and_bumps_revision(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path)
    imported = _import(hub, repo)
    plan = hub.store.get_plan(imported["plan_id"])
    first_hash, first_revision = plan.content_hash, plan.revision
    assert first_revision == 1

    plan.tasks[0].objective = "a materially different objective"
    hub.store.save_plan(plan)
    assert plan.revision == first_revision + 1
    assert plan.content_hash != first_hash

    stale = hub.store.get_approval(imported["approval_id"])
    assert stale.status == "expired"
    with pytest.raises(ConflictError):
        hub.planning.approve_plan(imported["plan_id"],
                                  approval_id=imported["approval_id"])

    fresh = hub.approvals.ensure_plan_approval(plan)
    assert fresh.approval_id != imported["approval_id"]
    assert fresh.details["plan_hash"] == plan.content_hash
    assert fresh.details["plan_revision"] == plan.revision
    hub.close()


def test_approval_is_idempotent_and_creates_exactly_one_run(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path)
    imported = _import(hub, repo)
    _settle_architecture(hub, imported["approval_id"])
    first = hub.decide_approval(imported["approval_id"], approve=True)["outcome"]
    second = hub.decide_approval(imported["approval_id"], approve=True)["outcome"]
    assert first["run_id"] == second["run_id"]
    assert second.get("idempotent") is True
    assert len(hub.runs()) == 1
    assert len(hub.tasks(run_id=first["run_id"])) == 3
    hub.close()


def test_concurrent_approvals_create_exactly_one_run(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path)
    imported = _import(hub, repo)
    _settle_architecture(hub, imported["approval_id"])
    results: list[str] = []
    errors: list[Exception] = []

    def approve() -> None:
        try:
            outcome = hub.decide_approval(imported["approval_id"], approve=True)["outcome"]
            results.append(outcome["run_id"])
        except Exception as exc:  # noqa: BLE001 - expected ConflictError
            errors.append(exc)

    threads = [threading.Thread(target=approve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(hub.runs()) == 1
    assert results and len(set(results)) == 1
    hub.close()


def test_failure_during_task_creation_rolls_back_everything(tmp_path: Path, monkeypatch):
    repo, hub, _ = make_project(tmp_path)
    imported = _import(hub, repo)
    _settle_architecture(hub, imported["approval_id"])

    def boom(*_args, **_kwargs):
        raise RuntimeError("injected task insert failure")

    monkeypatch.setattr(hub.store, "_insert_tasks_conn", boom)
    with pytest.raises(RuntimeError):
        hub.planning.approve_plan(imported["plan_id"],
                                  approval_id=imported["approval_id"])
    assert hub.store.get_plan(imported["plan_id"]).status == "pending_approval"
    assert hub.store.get_approval(imported["approval_id"]).status == "pending"
    assert hub.runs() == []
    hub.close()


def test_chain_binding_targets_user_tasks_and_keeps_system_review(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path)
    hub.set_agent_model("mock_worker", "mock-model")
    hub.set_agent_model("mock_verifier", "mock-model")
    imported = hub.import_plan(
        {
            "plan_version": 1, "goal": "one user node",
            "constraints": [], "tasks": [{
                "task_id": "IMP", "title": "implement", "type": "implementation",
                "objective": "do work", "scope": ["calc.py"],
                "dependencies": [], "verification_commands": [],
            }],
        },
        main_agent_id="mock_main", model="mock-model",
    )
    plan = hub.store.get_plan(imported["plan_id"])
    system_ids = [task.task_id for task in plan.tasks if task.origin == "system"]
    assert system_ids, "contract should have added a REVIEW node"
    out = hub.configure_chain(
        goal="one user node", plan_id=imported["plan_id"],
        stages=[{"agent_id": "mock_worker", "model": "mock-model",
                 "type": "implementation"}],
        save_only=True,
    )
    assert out["status"] == "bound"
    assert out["stages"][0]["task_id"] == "IMP"
    bound = hub.store.get_plan(imported["plan_id"])
    assert bound.revision == plan.revision + 1
    imp = next(t for t in bound.tasks if t.task_id == "IMP")
    assert imp.suggested_agent == "mock_worker"
    review = next(t for t in bound.tasks if t.task_id == system_ids[0])
    assert review.origin == "system"
    assert review.suggested_agent != "mock_worker"
    hub.close()


def test_plan_request_has_attempt_and_request_identity(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path)
    imported = hub.request_plan("plan identity", main_agent_id="mock_main",
                                model="mock-model")
    attempts = hub.store.list_attempts(f"{imported['plan_id']}:PLAN")
    assert attempts and attempts[0]["purpose"] == "plan"
    assert attempts[0]["request_id"]
    assert attempts[0]["binding"]["marker"].startswith("AGENTMESH_GUI_MARKER_PLAN_")
    hub.close()
