"""Canonical workspace data paths for a project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..util import ensure_dir


@dataclass
class WorkspacePaths:
    root: Path

    @property
    def base(self) -> Path:
        return self.root / ".agentloop"

    @property
    def db(self) -> Path:
        return self.base / "state.db"

    @property
    def project_yaml(self) -> Path:
        return self.base / "project.yaml"

    @property
    def config_json(self) -> Path:
        return self.base / "config.json"

    @property
    def agents(self) -> Path:
        return self.base / "agents"

    @property
    def tasks(self) -> Path:
        return self.base / "tasks"

    @property
    def runs(self) -> Path:
        return self.base / "runs"

    @property
    def handoffs(self) -> Path:
        return self.base / "handoffs"

    @property
    def artifacts(self) -> Path:
        return self.base / "artifacts"

    @property
    def patches(self) -> Path:
        return self.base / "patches"

    @property
    def reports(self) -> Path:
        return self.base / "reports"

    @property
    def approvals(self) -> Path:
        return self.base / "approvals"

    @property
    def checkpoints(self) -> Path:
        return self.base / "checkpoints"

    @property
    def audit(self) -> Path:
        return self.base / "audit"

    @property
    def relay(self) -> Path:
        return self.base / "relay"

    @property
    def planning(self) -> Path:
        return self.base / "planning"

    @property
    def conversations(self) -> Path:
        return self.base / "conversations"

    @property
    def cache(self) -> Path:
        return self.base / "cache"

    def ensure(self) -> None:
        for path in (
            self.base, self.agents, self.tasks, self.runs, self.handoffs, self.artifacts,
            self.patches, self.reports, self.approvals, self.checkpoints, self.audit,
            self.relay, self.planning, self.conversations, self.cache,
        ):
            ensure_dir(path)

    def task_dir(self, uid: str) -> Path:
        return self.tasks / uid.replace(":", "_")

    def run_dir(self, run_id: str) -> Path:
        return self.runs / run_id

    def worktree_dir(self, run_id: str, task_id: str) -> Path:
        return self.run_dir(run_id) / "worktrees" / task_id

    def verify_dir(self, run_id: str, task_id: str) -> Path:
        return self.run_dir(run_id) / "verify" / task_id

    def patch_path(self, uid: str) -> Path:
        return self.patches / f"{uid.replace(':', '_')}.patch"

    def report_path(self, name: str) -> Path:
        return self.reports / name
