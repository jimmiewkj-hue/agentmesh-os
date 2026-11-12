"""Summary context freshness (2026-11-07 defect #8).

The summary prompt used to shrink by dropping list tails, so the newest round
(R2_T001/R2_REVIEW) was removed before round-1 tasks and the main node judged
on stale evidence.  Latest round now comes first and survives the budget.
"""
from __future__ import annotations

from agentloop_hub.config import HubConfig
from agentloop_hub.control.context import ContextBuilder, task_round_id
from agentloop_hub.models import Project, ResultPackage, Task, TaskSpec
from agentloop_hub.storage.db import Database
from agentloop_hub.storage.repo import Store


def _task(task_id: str, *, agent: str = "worker") -> Task:
    return Task(uid=f"run:{task_id}", project_id="prj", plan_id="plan",
                run_id="run", spec=TaskSpec(task_id=task_id, title=f"任务 {task_id}",
                                            objective="o", scope=["src/**"]),
                status="completed", attempts=1, assigned_agent=agent)


def _payload(task: Task, summary: str) -> dict:
    return {
        "task_id": task.spec.task_id, "agent_id": task.assigned_agent,
        "status": "completed", "summary": summary,
        "findings": [], "files_read": [], "files_changed": [f"src/f{i}.py" for i in range(60)],
        "commands_run": [], "tool_calls": [],
        "tests": [{"command": "pytest -q", "result": "passed"}] * 12,
        "evidence": [{"file": "src/a.py"}], "risks": ["r"] * 12,
        "unresolved_items": ["u"] * 15, "artifacts": [],
        "token_usage": {}, "cost_estimate": {}, "recommended_next_action": [],
    }


def test_task_round_id_parses_prefixed_ids():
    assert task_round_id("T001") == 1
    assert task_round_id("R2_T001") == 2
    assert task_round_id("R10_REVIEW") == 10
    assert task_round_id("SUMMARY") == 1


def test_summary_context_keeps_latest_round_under_budget(tmp_path):
    store = Store(Database(tmp_path / "s.db"))
    project = Project(project_id="prj", name="p", root=str(tmp_path),
                      workspace_id="w", repository_id="r", worktree_id="t")
    store.save_project(project)
    builder = ContextBuilder(store, project, HubConfig(context_summary_tokens=1500))
    tasks = []
    for i in range(1, 9):
        task = _task(f"T{i:03d}")
        tasks.append(task)
        store.save_result(task, ResultPackage(
            _payload(task, f"第 1 轮旧成果 {i} " + "x" * 3000)), valid=True)
    for task_id in ("R2_T001", "R2_REVIEW"):
        task = _task(task_id, agent="main")
        tasks.append(task)
        store.save_result(task, ResultPackage(
            _payload(task, f"最新轮次成果 {task_id}：已关闭 B1/B3")), valid=True)
    ctx = builder.summary_context(
        run={"run_id": "run"}, plan={"goal": "g", "constraints": []},
        tasks=tasks,
        changes=[
            {"change_id": f"c{i}", "task_uid": f"run:T{i:03d}", "files": [f"src/f{i}.py"]}
            for i in range(1, 9)
        ] + [{"change_id": "c20", "task_uid": "run:R2_T001",
              "files": ["src/layout.py"]}],
    )
    assert ctx["latest_round"] == 2
    assert ctx["rounds_present"] == [2, 1]
    ids = [row["task_id"] for row in ctx.get("tasks", [])]
    # Newest round first; both latest tasks survive the token cap.
    assert set(ids[:2]) == {"R2_T001", "R2_REVIEW"}
    newest = next(row for row in ctx["tasks"] if row["task_id"] == "R2_T001")
    assert "最新轮次成果" in newest["summary"]
    assert ctx.get("_truncated") is True
    # Per-entry fields are bounded before any entry is dropped.
    assert len(newest["files_changed"]) <= 30
    assert len(newest["unresolved_items"]) <= 12
    # Changes are latest-round-first too.
    assert ctx.get("changes")
    assert ctx["changes"][0]["task_uid"] == "run:R2_T001"


def test_cap_never_drops_the_first_entry(tmp_path):
    store = Store(Database(tmp_path / "s.db"))
    project = Project(project_id="prj", name="p", root=str(tmp_path),
                      workspace_id="w", repository_id="r", worktree_id="t")
    store.save_project(project)
    builder = ContextBuilder(store, project, HubConfig(context_summary_tokens=200))
    value = {"items": [{"id": "newest", "text": "y" * 500},
                       {"id": "older", "text": "z" * 500}]}
    capped = builder._cap(value, 200)
    assert capped["items"][0]["id"] == "newest"