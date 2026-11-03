"""File-scope leases: prevents two writable tasks from claiming the same area."""

from __future__ import annotations

from ..models import Task, pattern_overlap
from ..storage.repo import Store


class LeaseManager:
    def __init__(self, store: Store):
        self.store = store

    def conflicts(self, project_id: str, task: Task) -> list[dict]:
        if not task.spec.writable:
            return []
        active = self.store.active_leases(project_id)
        conflicts = []
        for lease in active:
            if lease["task_uid"] == task.uid:
                continue
            overlap = pattern_overlap([lease["path_pattern"]], task.spec.scope)
            if overlap:
                conflicts.append({
                    "task_uid": lease["task_uid"],
                    "agent_id": lease["agent_id"],
                    "path_pattern": lease["path_pattern"],
                    "overlap": overlap,
                })
        return conflicts

    def acquire(self, project_id: str, task: Task) -> None:
        if not task.spec.writable:
            return
        for pattern in task.spec.scope:
            self.store.acquire_lease(project_id, pattern, task.uid, task.assigned_agent, "write")

    def release(self, task_uid: str) -> None:
        self.store.release_task_leases(task_uid)
