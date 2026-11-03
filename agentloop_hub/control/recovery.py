"""Crash recovery: interrupted tasks, leases, worktrees and checkpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..models import Project, TaskStatus
from ..storage.repo import Store
from ..workspace import changed_paths, list_worktrees
from ..workspace.leases import LeaseManager
from .audit import AuditService
from .budget import BudgetManager
from .checkpoints import CheckpointService
from .paths import WorkspacePaths


class RecoveryService:
    def __init__(self, store: Store, project: Project, paths: WorkspacePaths,
                 audit: AuditService, budget: BudgetManager, checkpoints: CheckpointService):
        self.store = store
        self.project = project
        self.paths = paths
        self.audit = audit
        self.budget = budget
        self.checkpoints = checkpoints
        self.leases = LeaseManager(store)

    def inspect(self) -> dict[str, Any]:
        runs = self.store.list_runs(self.project.project_id, limit=20)
        active_runs = [r for r in runs if r["status"] in ("running", "paused", "waiting_user", "merging")]
        tasks = []
        orphan_worktrees = []
        for run in active_runs:
            for task in self.store.list_tasks(run["run_id"]):
                if task.status in (TaskStatus.RUNNING.value, TaskStatus.QUEUED.value,
                                   TaskStatus.WAITING_USER.value, TaskStatus.WAITING_AGENT.value):
                    result = self.store.get_result(task.uid)
                    tasks.append({
                        "run_id": run["run_id"], "task_uid": task.uid, "task_id": task.task_id,
                        "status": task.status, "agent": task.assigned_agent,
                        "workdir": task.workdir, "result_exists": bool(result),
                        "result_valid": bool(result and result["valid"]),
                        "workdir_exists": bool(task.workdir and Path(task.workdir).exists()),
                    })
        for entry in list_worktrees(self.project.root):
            path = entry.get("worktree", "")
            if ".agentloop" in path and Path(path).exists():
                known = any(t.get("workdir") == path for t in tasks)
                if not known:
                    orphan_worktrees.append(path)
        return {
            "project_id": self.project.project_id,
            "active_runs": [
                {"run_id": r["run_id"], "status": r["status"], "plan_id": r["plan_id"]}
                for r in active_runs
            ],
            "tasks": tasks,
            "orphan_worktrees": orphan_worktrees,
            "pending_approvals": [a.to_dict() for a in self.store.list_approvals(
                self.project.project_id, status="pending")],
            "last_checkpoint": self.checkpoints.latest(),
            "budget": self.budget.snapshot(),
            "audit": self.audit.verify(),
        }

    def recover(self, *, run_id: str = "", auto_resume: bool = False) -> dict[str, Any]:
        runs = self.store.list_runs(self.project.project_id, limit=20)
        if run_id:
            runs = [r for r in runs if r["run_id"] == run_id]
        recovered_tasks = []
        released_leases = 0
        for run in runs:
            if run["status"] not in ("running", "paused", "waiting_user", "merging"):
                continue
            tasks = self.store.list_tasks(run["run_id"])
            for task in tasks:
                if task.status == TaskStatus.RUNNING.value:
                    result = self.store.get_result(task.uid)
                    if result and result["valid"]:
                        self.store.set_task_status(task.uid, TaskStatus.VERIFYING.value,
                                                   error="恢复：从已有成果包继续", force=True)
                        recovered_tasks.append({"task_uid": task.uid, "action": "verify_existing"})
                    else:
                        changes = changed_paths(task.workdir) if task.workdir and Path(task.workdir).exists() else []
                        self.store.set_task_status(task.uid, TaskStatus.PAUSED.value,
                                                   error="恢复：任务被中断，需人工确认后继续", force=True)
                        self.leases.release(task.uid)
                        released_leases += 1
                        recovered_tasks.append({
                            "task_uid": task.uid, "action": "paused",
                            "pending_changes": [c["path"] for c in changes][:20],
                        })
                elif task.status in (TaskStatus.FAILED.value, TaskStatus.CANCELED.value):
                    self.leases.release(task.uid)
            if run["status"] in ("running", "merging"):
                self.store.update_run(run["run_id"], status="paused", error="程序中断后恢复")
            if auto_resume:
                for task in self.store.list_tasks(run["run_id"]):
                    if task.status in (TaskStatus.PAUSED.value, TaskStatus.BLOCKED.value):
                        if task.status == TaskStatus.PAUSED.value and task.workdir and \
                                Path(task.workdir).exists() and changed_paths(task.workdir):
                            # Never auto-rerun a task that already modified files.
                            continue
                        self.store.set_task_status(task.uid, TaskStatus.QUEUED.value, force=True)
                        self.store.update_task(task.uid, error="")
                        recovered_tasks.append({"task_uid": task.uid, "action": "requeued"})
        self.audit.log("control-plane", "recovery.completed",
                       payload={"tasks": recovered_tasks, "released_leases": released_leases,
                                "auto_resume": auto_resume})
        self.checkpoints.create("recovery", run_id=run_id,
                                extra={"tasks": recovered_tasks})
        return {
            "status": "recovered",
            "tasks": recovered_tasks,
            "released_leases": released_leases,
            "report": self.inspect(),
        }
