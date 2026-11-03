from __future__ import annotations

from agentloop_hub.config import HubConfig
from agentloop_hub.control.scheduler import TaskScheduler
from agentloop_hub.models import Task, TaskSpec


def make_task(task_id: str, *, status: str = "queued", priority: int = 5,
              deps: list[str] | None = None, attempts: int = 0,
              retry_limit: int = 0) -> Task:
    spec = TaskSpec(
        task_id=task_id, title=task_id, objective=task_id, type="analysis",
        dependencies=deps or [], priority=priority, retry_limit=retry_limit,
    )
    return Task(uid=f"run:{task_id}", project_id="p", plan_id="plan", run_id="run",
                spec=spec, status=status, attempts=attempts)


def test_priority_order_and_dependency_gate():
    scheduler = TaskScheduler(HubConfig(max_failures_before_circuit_break=3))
    tasks = [
        make_task("low", priority=1),
        make_task("high", priority=9),
        make_task("mid", priority=5),
        make_task("dependent", priority=10, deps=["high"]),
    ]
    plan = scheduler.evaluate(tasks, capacity=3)
    assert [t.task_id for t in plan.ready] == ["high", "mid", "low"]
    assert all(t.task_id != "dependent" for t in plan.ready)

    tasks[1].status = "completed"
    plan2 = scheduler.evaluate(tasks, capacity=3)
    assert [t.task_id for t in plan2.ready][0] == "dependent"


def test_blocked_dependents_and_circuit_breaker():
    scheduler = TaskScheduler(HubConfig(max_failures_before_circuit_break=2))
    failed = make_task("failed", status="failed", attempts=2, retry_limit=0)
    blocked = make_task("blocked", deps=["failed"])
    other_failed = make_task("f2", status="failed", attempts=1, retry_limit=0)
    plan = scheduler.evaluate([failed, blocked, other_failed], capacity=3)
    assert [t.task_id for t in plan.blocked] == ["blocked"]
    assert plan.should_break is True
    assert {t.task_id for t in plan.final_failures} == {"failed", "f2"}


def test_terminal_work_detection():
    scheduler = TaskScheduler(HubConfig())
    assert scheduler.has_work([make_task("q")])
    assert not scheduler.has_work([make_task("c", status="completed")])
    summary = scheduler.priority_summary([make_task("a", priority=3), make_task("b", priority=8)])
    assert summary[0]["task_id"] == "b"
