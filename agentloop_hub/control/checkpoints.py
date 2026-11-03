"""Checkpoint service: durable snapshots at every critical boundary."""

from __future__ import annotations

import json
from typing import Any

from ..models import Checkpoint, Project
from ..storage.repo import Store
from ..util import atomic_write_text, ensure_dir, new_id, now_iso
from .budget import BudgetManager
from .paths import WorkspacePaths


class CheckpointService:
    def __init__(self, store: Store, project: Project, paths: WorkspacePaths,
                 budget: BudgetManager):
        self.store = store
        self.project = project
        self.paths = paths
        self.budget = budget

    def create(self, kind: str, *, run_id: str = "", task_uid: str = "",
               extra: dict[str, Any] | None = None) -> Checkpoint:
        run = self.store.get_run(run_id) if run_id else None
        tasks = self.store.list_tasks(run_id) if run_id else []
        snapshot = {
            "kind": kind,
            "created_at": now_iso(),
            "run": {
                "run_id": run["run_id"], "status": run["status"], "plan_id": run["plan_id"],
                "base_commit": run["base_commit"], "summary": run["summary"],
            } if run else None,
            "tasks": [
                {
                    "uid": t.uid, "task_id": t.task_id, "status": t.status,
                    "assigned_agent": t.assigned_agent, "attempts": t.attempts,
                    "patch_path": t.patch_path, "verification_status": t.verification_status,
                }
                for t in tasks
            ],
            "pending_approvals": [
                {
                    "approval_id": a.approval_id, "kind": a.kind, "subject": a.subject,
                }
                for a in self.store.list_approvals(self.project.project_id, status="pending")
            ],
            "budget": self.budget.snapshot(run_id=run_id),
            "extra": extra or {},
        }
        checkpoint = Checkpoint(
            checkpoint_id=new_id("ckpt"), project_id=self.project.project_id,
            kind=kind, snapshot=snapshot, run_id=run_id, task_uid=task_uid,
            created_at=snapshot["created_at"],
        )
        self.store.add_checkpoint(checkpoint)
        target = ensure_dir(self.paths.checkpoints) / f"{checkpoint.checkpoint_id}.json"
        atomic_write_text(target, json.dumps(snapshot, ensure_ascii=False, indent=2))
        return checkpoint

    def latest(self, *, run_id: str = "") -> dict[str, Any] | None:
        return self.store.latest_checkpoint(self.project.project_id, run_id=run_id)

    def list(self, *, run_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        return self.store.list_checkpoints(self.project.project_id, run_id=run_id, limit=limit)
