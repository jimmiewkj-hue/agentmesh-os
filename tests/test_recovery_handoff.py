from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentloop_hub.models import Approval, MemoryType, TaskStatus
from conftest import default_plan, make_project


def test_recovery_pauses_interrupted_task_and_releases_lease(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    plan = default_plan(str(tmp_path))
    result = hub.import_plan(plan)
    hub.decide_approval(result["approval_id"], approve=True)
    run_id = hub.latest_run()["run_id"]
    hub.store.update_run(run_id, status="running")
    tasks = hub.store.list_tasks(run_id)
    task = next(t for t in tasks if t.task_id == "T003")
    # Simulate a process killed after work started.
    hub.store.set_task_status(task.uid, TaskStatus.RUNNING.value, force=True)
    hub.store.update_task(task.uid, workdir=str(tmp_path / "fake_wt"), task_dir=str(tmp_path / "td"))
    hub.store.acquire_lease(hub.project.project_id, "calc.py", task.uid, "mock_worker")
    report = hub.recover()
    assert any(r["action"] == "paused" for r in report["tasks"])
    fresh = hub.store.get_task(task.uid)
    assert fresh.status == "paused"
    assert hub.store.active_leases(hub.project.project_id) == []
    assert hub.audit_verify()["ok"]
    hub.close()


def test_recovery_does_not_rerun_modified_workdir(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    plan = default_plan(str(tmp_path))
    result = hub.import_plan(plan)
    hub.decide_approval(result["approval_id"], approve=True)
    run_id = hub.latest_run()["run_id"]
    hub.store.update_run(run_id, status="running")
    task = next(t for t in hub.store.list_tasks(run_id) if t.task_id == "T003")
    wt = tmp_path / "wt"
    wt.mkdir()
    hub.store.set_task_status(task.uid, TaskStatus.RUNNING.value, force=True)
    hub.store.update_task(task.uid, workdir=str(wt))
    # A paused task with changes must not be auto-requeued.
    hub.store.set_task_status(task.uid, TaskStatus.PAUSED.value, force=True)
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=wt, check=True)
    (wt / "calc.py").write_text("changed", encoding="utf-8")
    report = hub.recover(auto_resume=True)
    fresh = hub.store.get_task(task.uid)
    assert fresh.status == "paused"
    hub.close()


def test_memory_service_conflict_and_search(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    m1 = hub.memory_add(type="project_fact", title="tech", body="python")
    m2 = hub.memory_add(type="project_fact", title="tech", body="node")
    assert m2["status"] == "conflict"
    assert hub.memory_list(status="conflict")
    assert hub.memory_search("node")
    hub.memory_resolve(m2["memory_id"], keep=True)
    assert hub.store.get_memory(m1["memory_id"]).status == "superseded"
    hub.memory_update(m2["memory_id"], body="node v20")
    assert hub.store.get_memory(m2["memory_id"]).body == "node v20"
    hub.memory_delete(m2["memory_id"])
    assert hub.store.get_memory(m2["memory_id"]) is None
    hub.close()


def test_handoff_package_and_main_switch(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    plan = default_plan(str(tmp_path))
    result = hub.import_plan(plan)
    hub.decide_approval(result["approval_id"], approve=True)
    run_id = hub.latest_run()["run_id"]
    # Start an implementation task then switch to a new main node.
    hub.start_run(run_id, max_parallel=2)
    package = hub.handoff()["package"]
    assert package["project_goal"] == "fix add and scan readme"
    assert package["confirmed_requirements"]
    assert package["current_phase"]
    assert isinstance(package["open_tasks"], list)
    assert isinstance(package["completed_tasks"], list)
    switched = hub.handoff(to_agent_id="mock_verifier", model="new-model", note="handoff")
    files = switched["files"]
    assert Path(files["yaml"]).exists() and Path(files["markdown"]).exists()
    main = hub.get_main()
    assert main["agent_id"] == "mock_verifier" and main["model"] == "new-model"
    assert hub.main_history()[0]["agent_id"] == "mock_verifier"
    text = Path(files["yaml"]).read_text(encoding="utf-8")
    assert "confirmed_requirements" in text and "pending_approvals" in text
    hub.close()


def test_handoff_missing_agent(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    with pytest.raises(Exception):
        hub.handoff(to_agent_id="nope")
    hub.close()
