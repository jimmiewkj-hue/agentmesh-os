"""S05/S08: durable outbox, persistent reconcile wake-ups and controller ownership."""

from __future__ import annotations

from pathlib import Path

from hub_test_support import default_plan, make_project

from agentloop_hub.config import HubConfig
from agentloop_hub.hub import Hub
from agentloop_hub.models import ResultPackage, TaskStatus


def test_outbox_survives_a_failed_bus_publish(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path)

    def boom(*_args, **_kwargs):
        raise RuntimeError("bus unavailable")

    original = hub.bus.emit
    hub.bus.emit = boom  # type: ignore[method-assign]
    try:
        hub.store.emit("workflow", {"phase": "probe"}, correlation_id="run-probe")
        pending = hub.store.db.query(
            "SELECT COUNT(*) AS n FROM outbox WHERE project_id=? AND published=0",
            (hub.project.project_id,))
        assert int(pending[0]["n"]) >= 1
    finally:
        hub.bus.emit = original  # type: ignore[method-assign]
    drained = hub.store.drain_outbox()
    assert drained["delivered"] >= 1
    kinds = [e["kind"] for e in hub.bus_events(limit=500)["events"]]
    assert "workflow" in kinds
    hub.close()


def test_plan_approval_commits_business_state_and_outbox_together(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path)
    imported = hub.import_plan(default_plan(str(repo)),
                               main_agent_id="mock_main", model="mock-model")
    original = hub.bus.emit
    hub.bus.emit = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down"))  # type: ignore[assignment,method-assign]
    try:
        approved = hub.decide_approval(imported["approval_id"], approve=True)
    finally:
        hub.bus.emit = original  # type: ignore[method-assign]
    run_id = approved["outcome"]["run_id"]
    assert hub.store.get_run(run_id)["status"] == "ready"
    assert len(hub.store.list_tasks(run_id)) == 3
    pending = hub.store.db.query(
        "SELECT kind FROM outbox WHERE project_id=? AND published=0",
        (hub.project.project_id,))
    assert {"governance", "workflow"} <= {row["kind"] for row in pending}
    hub.store.drain_outbox()
    kinds = {e["kind"] for e in hub.bus_events(limit=500)["events"]}
    assert {"governance", "workflow"} <= kinds
    hub.close()


def test_second_controller_does_not_release_a_live_owners_lease(tmp_path: Path):
    repo, hub1, _ = make_project(tmp_path)
    task_uid = "run_live:T1"
    hub1.store.acquire_lease(hub1.project.project_id, "calc.py", task_uid,
                             "mock_worker", owner_id=hub1.instance_id)
    hub1.store.create_run(hub1.project.project_id, "plan-x", main_agent="mock_main",
                          model="mock-model", base_commit=hub1.project.commit,
                          base_branch="main", status="running",
                          run_id="run_live")
    hub1.store.update_run("run_live", owner_id=hub1.instance_id)
    cfg = HubConfig(auto_reconcile_on_startup=False)
    hub2 = Hub(repo, config=cfg, auto_discover=False)
    try:
        active = {row["task_uid"] for row in hub2.store.active_leases(
            hub2.project.project_id)}
        assert task_uid in active, "a live controller's lease was released"
        blocked = hub2.runner.start("run_live")
        assert blocked["status"] == "blocked", blocked
        assert hub2.store.get_run("run_live")["owner_id"] == hub1.instance_id
    finally:
        hub1.store.deactivate_controller_instance(hub1.instance_id)
        hub2.close()
        hub1.close()
    cfg2 = HubConfig(auto_reconcile_on_startup=False)
    hub3 = Hub(repo, config=cfg2, auto_discover=False)
    try:
        active = {row["task_uid"] for row in hub3.store.active_leases(
            hub3.project.project_id)}
        assert task_uid not in active
    finally:
        hub3.close()


def test_reconcile_todo_is_deduplicated_and_persisted(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path)
    run_id = "run_todo"
    hub.store.create_run(hub.project.project_id, "plan-x", main_agent="mock_main",
                         model="mock-model", base_commit=hub.project.commit,
                         base_branch="main", status="paused", run_id=run_id)
    first = hub.store.enqueue_reconcile(run_id, "startup_recovery")
    second = hub.store.enqueue_reconcile(run_id, "startup_recovery")
    assert first == second
    assert len(hub.store.pending_reconcile()) == 1
    hub.store.complete_reconcile(first)
    assert hub.store.pending_reconcile() == []
    hub.close()
