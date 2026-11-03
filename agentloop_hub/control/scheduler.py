"""Task Scheduler: dependency-aware, priority-ordered dispatch decisions.

需求整理-v1 模块 4 要求任务拆分、依赖、优先级、Agent 选择与失败重试。
任务拆分由主节点规划（planning.py）完成；本模块负责运行期确定性调度：

* 只把依赖已完成的 ``queued`` 任务放入就绪队列；
* 同一层按 ``priority`` 降序、``task_id`` 稳定排序；
* 前置失败/取消/拒绝的任务变为 ``blocked``；
* 最终失败数达到阈值时触发熔断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..config import HubConfig
from ..models import Task, TaskStatus

TERMINAL = {
    TaskStatus.COMPLETED.value,
    TaskStatus.FAILED.value,
    TaskStatus.REJECTED.value,
    TaskStatus.CANCELED.value,
}

FAILED_DEPENDENCY_STATUSES = {
    TaskStatus.FAILED.value,
    TaskStatus.CANCELED.value,
    TaskStatus.REJECTED.value,
}


@dataclass
class DispatchPlan:
    ready: list[Task]
    blocked: list[Task]
    final_failures: list[Task]
    should_break: bool


class TaskScheduler:
    def __init__(self, config: HubConfig):
        self.config = config

    def evaluate(self, tasks: Iterable[Task], *, capacity: int) -> DispatchPlan:
        task_list = list(tasks)
        by_id = {t.task_id: t for t in task_list}
        blocked: list[Task] = []
        ready: list[Task] = []
        for task in task_list:
            if task.status != TaskStatus.QUEUED.value:
                continue
            failed_dep = next(
                (by_id[dep] for dep in task.spec.dependencies
                 if dep in by_id and by_id[dep].status in FAILED_DEPENDENCY_STATUSES),
                None,
            )
            if failed_dep:
                blocked.append(task)
                continue
            if all(
                dep in by_id and by_id[dep].status == TaskStatus.COMPLETED.value
                for dep in task.spec.dependencies
            ):
                ready.append(task)
        ready.sort(key=lambda t: (-int(t.spec.priority), t.created_at, t.task_id))
        final_failures = [
            t for t in task_list
            if t.status == TaskStatus.FAILED.value and t.attempts > t.spec.retry_limit
        ]
        should_break = len(final_failures) >= max(1, int(self.config.max_failures_before_circuit_break))
        return DispatchPlan(
            ready=ready[: max(0, int(capacity))],
            blocked=blocked,
            final_failures=final_failures,
            should_break=should_break,
        )

    @staticmethod
    def has_work(tasks: Iterable[Task]) -> bool:
        return any(t.status not in TERMINAL for t in tasks)

    def priority_summary(self, tasks: Iterable[Task]) -> list[dict[str, object]]:
        return [
            {"task_id": t.task_id, "status": t.status, "priority": int(t.spec.priority),
             "dependencies": list(t.spec.dependencies)}
            for t in sorted(tasks, key=lambda x: (-int(x.spec.priority), x.task_id))
        ]


__all__ = ["TERMINAL", "DispatchPlan", "TaskScheduler"]
