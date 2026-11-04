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

    def release_stale(self, project_id: str, *, keep_run_ids: set[str] | None = None,
                      include_running: bool = False) -> list[str]:
        """Release leases whose run is no longer active.

        An aborted control plane (crash, kill, power loss) never reaches the
        ``release`` call, so the write scope stays claimed and every later
        writable task in the project is blocked by a phantom conflict.
        """
        keep = set(keep_run_ids or set())
        released: list[str] = []
        for lease in self.store.active_leases(project_id):
            task_uid = str(lease.get("task_uid") or "")
            run_id = task_uid.split(":", 1)[0] if ":" in task_uid else ""
            if run_id and run_id in keep:
                continue
            run = self.store.get_run(run_id) if run_id else None
            if run and run.get("status") in ("running", "ready", "pending_acceptance",
                                             "pending_merge", "merging", "waiting_user"):
                # At control-plane start any "running" run belongs to a previous
                # process; callers pass include_running=True there.
                if not include_running:
                    continue
            self.store.release_task_leases(task_uid)
            released.append(task_uid)
        return sorted(set(released))
