"""S05: cancellation is irreversible and reconcile continues existing work."""

from __future__ import annotations

from pathlib import Path

from hub_test_support import default_plan, make_project

from agentloop_hub.models import ResultPackage, TaskStatus


def _approved_run(hub, repo: Path) -> str:
    imported = hub.import_plan(default_plan(str(repo)), main_agent_id="mock_main",
                               model="mock-model")
    approved = hub.decide_approval(imported["approval_id"], approve=True)
    return approved["outcome"]["run_id"]


def _summary() -> dict:
    return {
        "summary_version": 1, "completion": "done", "changes": ["calc.py"],
        "verification": "passed", "risks": [], "merge_recommendation": "approve",
        "next_actions": [], "goal_status": "complete",
    }


def test_late_summary_cannot_revive_a_canceled_run(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path)
    run_id = _approved_run(hub, repo)
    epoch = int(hub.store.get_run(run_id)["epoch"])
    hub.runner.cancel(run_id, "user stop")
    result = hub.runner.finalize_summary(run_id, _summary(), epoch=epoch)
    assert result["status"] == "canceled"
    run = hub.store.get_run(run_id)
    assert run["status"] == "canceled"
    assert run["acceptance_status"] == "pending"
    assert not [a for a in hub.approvals_pending() if a["kind"] == "acceptance"]
    assert Path(result["stale_summary_artifact"]).exists()
    # A canceled run is not restartable.
    assert hub.runner.start(run_id)["status"] == "canceled"
    hub.close()


def test_resume_finishes_a_verifying_task_without_rerunning_it(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path)
    run_id = _approved_run(hub, repo)
    task = hub.store.list_tasks(run_id)[0]
    payload = {
        "task_id": task.task_id, "agent_id": "mock_worker", "status": "completed",
        "summary": "scan done", "findings": [], "files_read": ["README.md"],
        "files_changed": [], "commands_run": [], "tool_calls": [],
        "tests": [], "evidence": [{"file": "README.md", "lines": "1",
                                   "description": "read"}],
        "risks": [], "unresolved_items": [], "artifacts": [],
        "token_usage": {}, "cost_estimate": {}, "recommended_next_action": [],
    }
    hub.store.update_task(task.uid, assigned_agent="mock_worker",
                          workdir=str(repo), task_dir=str(Path(repo) / ".agentloop" / "t"),
                          active_attempt_id="att-test")
    hub.store.create_attempt(
        attempt_id="att-test", project_id=hub.project.project_id,
        task_uid=task.uid, run_id=run_id, attempt_number=1,
        request_id="req-test", workdir=str(repo),
        result_path=str(Path(repo) / "result.json"),
    )
    hub.store.save_result(task, ResultPackage(payload), valid=True)
    hub.store.set_task_status(task.uid, TaskStatus.VERIFYING.value, force=True)
    hub.store.update_run(run_id, status="paused", error="interrupted")

    hub.runner.resume(run_id, background=False, max_parallel=1)

    row = hub.store.get_task(task.uid)
    assert row.status in (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value,
                          TaskStatus.BLOCKED.value), row
    # The implementation must not be sent again: there is still exactly one
    # attempt for this task.
    assert len(hub.store.list_attempts(task.uid)) == 1
    hub.close()


def test_reconcile_enqueue_reuses_the_same_todo_after_completion(tmp_path: Path):
    from hub_test_support import make_project

    _, hub, _ = make_project(tmp_path)
    store = hub.store
    first = store.enqueue_reconcile("run_x", "task_completed")
    second = store.enqueue_reconcile("run_x", "task_completed")
    assert first and first == second
    assert [t["todo_id"] for t in store.pending_reconcile()] == [first]

    store.complete_reconcile(first)
    done = store.db.query_one(
        "SELECT status FROM reconcile_todos WHERE todo_id=?", (first,))
    assert done["status"] == "done"

    # A later wake-up must revive the same row; a second row becoming done
    # would violate the (project, run, reason, status) unique key.
    third = store.enqueue_reconcile("run_x", "task_completed")
    assert third == first
    revived = store.db.query_one(
        "SELECT status, processed_at FROM reconcile_todos WHERE todo_id=?",
        (first,))
    assert revived["status"] == "pending"
    assert revived["processed_at"] == ""
    hub.close()


def test_complete_reconcile_survives_a_legacy_duplicate_done_row(tmp_path: Path):
    from hub_test_support import make_project

    _, hub, _ = make_project(tmp_path)
    store = hub.store
    older = store.enqueue_reconcile("run_y", "task_completed")
    store.complete_reconcile(older)
    # Legacy state: a second pending row for the same intent already exists.
    store.db.execute(
        "INSERT INTO reconcile_todos(todo_id, project_id, run_id, reason, status, "
        "created_at) VALUES('todo_legacy', ?, 'run_y', 'task_completed', 'pending', "
        "'2026-01-01T00:00:00Z')", (store.project_id,))
    store.complete_reconcile("todo_legacy")
    done = store.db.query(
        "SELECT todo_id FROM reconcile_todos WHERE project_id=? AND run_id='run_y' "
        "AND reason='task_completed' AND status='done'", (store.project_id,))
    assert [r["todo_id"] for r in done] == ["todo_legacy"]
    hub.close()


def test_resume_interrupted_requeues_without_counting_a_retry(tmp_path: Path):
    from hub_test_support import default_plan, make_project
    from agentloop_hub.util import new_id, now_iso

    repo, hub, _ = make_project(tmp_path)
    imported = hub.import_plan(default_plan(str(repo)), main_agent_id="mock_main",
                               model="mock-model")
    for approval in hub.approvals_pending():
        if approval["kind"] == "architecture":
            hub.decide_approval(approval["approval_id"], approve=True)
    run_id = hub.approve_plan(imported["plan_id"])["run_id"]
    uid = f"{run_id}:T001"
    attempt_id = "att_interrupted"
    hub.store.create_attempt(
        attempt_id=attempt_id, project_id=hub.project.project_id, task_uid=uid,
        run_id=run_id, attempt_number=1, request_id=new_id("req"),
        workdir=str(tmp_path / "wt"), result_path=str(tmp_path / "wt" / "result.json"))
    hub.store.update_task(uid, active_attempt_id=attempt_id, attempts=1)
    hub.store.set_task_status(uid, "paused", error="恢复：任务被中断", force=True)

    out = hub.resume_interrupted(uid)
    assert out["status"] == "queued"
    task = hub.store.get_task(uid)
    assert task.status == "queued"
    assert task.attempts == 0
    attempt = hub.store.get_attempt(attempt_id)
    assert attempt["status"] == "canceled"
    assert "原地续跑" in attempt["error"]

    hub.store.set_task_status(
        uid, "waiting_user", error="MANUAL_ACTION_REQUIRED: app busy", force=True)
    again = hub.resume_interrupted(uid)
    assert again["status"] == "queued"
    hub.close()
