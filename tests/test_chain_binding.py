"""Chain configuration binds a pending plan and never bypasses step-4 approval."""

from __future__ import annotations

from pathlib import Path

import pytest

from hub_test_support import default_plan, make_project

from agentloop_hub.hub import Hub


@pytest.fixture()
def env(tmp_path: Path):
    repo, hub, scenario_path = make_project(tmp_path)
    for agent_id, models in (
        ("mock_main", ["main-pro"]),
        ("mock_worker", ["worker-free"]),
        ("mock_verifier", ["verifier-pro"]),
    ):
        record = hub.get_agent(agent_id)
        hub.register_agent({
            "agent_id": agent_id, "name": record.name, "product": record.product,
            "adapter_type": record.adapter_type, "access_level": record.access_level,
            "adapter_config": record.adapter_config,
            "capabilities": record.capabilities, "models": models,
            "status": record.status,
        })
    return repo, hub, scenario_path


def _stages() -> list[dict]:
    return [
        {"task_id": "T001", "agent_id": "mock_worker", "model": "worker-free",
         "type": "analysis"},
        {"task_id": "T002", "agent_id": "mock_verifier", "model": "verifier-pro",
         "type": "review"},
        {"task_id": "T003", "agent_id": "mock_worker", "model": "worker-free",
         "type": "implementation"},
    ]


def test_bind_pending_plan_saves_without_creating_a_run(env):
    repo, hub, _ = env
    imported = hub.import_plan(default_plan(str(repo)), main_agent_id="mock_main",
                               model="main-pro")
    plan_id = imported["plan_id"]
    before = len(hub.runs())

    out = hub.configure_chain(goal="bind", stages=_stages(), plan_id=plan_id,
                              save_only=True)

    assert out["status"] == "bound"
    assert out["run_id"] == ""
    assert len(hub.runs()) == before
    stored = hub.plan(plan_id)
    by_id = {task["task_id"]: task for task in stored["tasks"]}
    assert by_id["T001"]["suggested_agent"] == "mock_worker"
    assert by_id["T001"]["model"] == "worker-free"
    assert by_id["T002"]["suggested_agent"] == "mock_verifier"
    assert by_id["T003"]["type"] == "implementation"


def test_approved_bound_plan_creates_run_with_pinned_nodes(env):
    repo, hub, _ = env
    imported = hub.import_plan(default_plan(str(repo)), main_agent_id="mock_main",
                               model="main-pro")
    plan_id = imported["plan_id"]
    hub.configure_chain(goal="bind", stages=_stages(), plan_id=plan_id,
                        save_only=True)

    for approval in hub.approvals_pending():
        if approval["kind"] == "architecture":
            hub.decide_approval(approval["approval_id"], approve=True)
    approved = hub.approve_plan(plan_id)
    run_id = approved["run_id"]

    tasks = {t["task_id"]: t for t in hub.tasks(run_id=run_id)}
    assert tasks["T001"]["assigned_agent"] == "mock_worker"
    assert tasks["T001"]["spec"]["model"] == "worker-free"
    assert tasks["T002"]["assigned_agent"] == "mock_verifier"
    assert tasks["T002"]["spec"]["model"] == "verifier-pro"
    # Approval only creates a ready run; execution still needs step 4 "start".
    assert hub.run_status(run_id)["run"]["status"] == "ready"


def test_standalone_save_only_creates_pending_plan_not_a_run(env):
    repo, hub, _ = env
    before = len(hub.runs())
    out = hub.configure_chain(
        goal="standalone scan",
        stages=[{"agent_id": "mock_worker", "model": "worker-free",
                 "type": "analysis"},
                {"agent_id": "mock_verifier", "model": "verifier-pro",
                 "type": "review"}],
        save_only=True,
    )
    assert out["status"] == "pending_approval"
    assert out["run_id"] == ""
    assert len(hub.runs()) == before
    assert out["plan_id"].startswith("plan_")


def test_bind_rejects_stage_count_mismatch(env):
    repo, hub, _ = env
    imported = hub.import_plan(default_plan(str(repo)), main_agent_id="mock_main",
                               model="main-pro")
    with pytest.raises(Exception) as info:
        hub.configure_chain(goal="bind", stages=_stages()[:1],
                            plan_id=imported["plan_id"], save_only=True)
    assert "不一致" in str(info.value)


def test_main_activity_and_synthetic_summary_are_visible(env):
    from agentloop_hub.util import write_json

    repo, hub, _ = env
    imported = hub.import_plan(default_plan(str(repo)), main_agent_id="mock_main",
                               model="main-pro")
    for approval in hub.approvals_pending():
        if approval["kind"] == "architecture":
            hub.decide_approval(approval["approval_id"], approve=True)
    run_id = hub.approve_plan(imported["plan_id"])["run_id"]
    summary_dir = Path(hub.paths.run_dir(run_id)) / "summary"
    summary_dir.mkdir(parents=True, exist_ok=True)
    write_json(summary_dir / "activity.json", {
        "phase": "summary", "round": 1, "status": "running",
        "agent_id": "mock_main", "model": "main-pro", "task_id": "SUMMARY",
        "started_at": "2026-11-05T00:00:00Z",
    })
    activity = hub.main_activity(run_id)
    assert activity["phase"] == "summary"
    assert activity["agent_id"] == "mock_main"

    write_json(summary_dir / "manual-action.json", {
        "kind": "agent_not_running", "agent_id": "mock_main",
        "message": "summary needs a human", "instructions": "open the app",
    })
    hub.store.update_run(run_id, status="waiting_user")
    actions = hub.manual_actions(run_id)
    summary = next(a for a in actions if a["task_id"] == "SUMMARY")
    assert summary["synthetic"] is True
    assert summary["agent_id"] == "mock_main"


def test_chain_draft_persists_across_hub_restart(env, tmp_path):
    repo, hub, _ = env
    draft = {"plan_id": "plan_x", "goal": "g", "policy": "balanced",
             "stages": [{"task_id": "T001", "agent_id": "mock_worker",
                         "model": "worker-free", "type": "analysis"}]}
    hub.save_chain_draft(draft)
    reopened = Hub(repo, auto_discover=False)
    loaded = reopened.chain_draft()
    assert loaded["plan_id"] == "plan_x"
    assert loaded["stages"][0]["model"] == "worker-free"


def test_reassigning_a_relay_wait_requeues_the_same_attempt(env):
    repo, hub, _ = env
    imported = hub.import_plan(default_plan(str(repo)), main_agent_id="mock_main",
                               model="main-pro")
    for approval in hub.approvals_pending():
        if approval["kind"] == "architecture":
            hub.decide_approval(approval["approval_id"], approve=True)
    run_id = hub.approve_plan(imported["plan_id"])["run_id"]
    uid = f"{run_id}:T003"
    hub.store.set_task_status(uid, "waiting_user", error="等待接力任务", force=True)
    hub.store.update_task(uid, attempts=1)
    updated = hub.update_task_binding(
        uid, agent_id="mock_verifier", model="verifier-pro", requeue=True)
    assert updated["status"] == "queued", updated
    task = hub.store.get_task(uid)
    assert task.assigned_agent == "mock_verifier"
    assert task.attempts == 0
    assert task.error == ""
    hub.close()
