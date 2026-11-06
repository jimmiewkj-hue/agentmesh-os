"""Merge approved patches into the user's working tree, with rollback."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import HubConfig
from ..errors import ApprovalRequiredError, ConflictError, NotFoundError, ValidationError
from ..models import ChangeStatus, Project, Task, TaskStatus
from ..storage.repo import Store
from ..util import now_iso
from ..workspace import (
    apply_patch, commit_all, patch_files, remove_worktree, reverse_patch, status_porcelain,
    run_git,
)
from .approvals import ApprovalService
from .audit import AuditService
from .checkpoints import CheckpointService
from .paths import WorkspacePaths
from .process_runner import run_command


class MergeService:
    def __init__(self, store: Store, project: Project, paths: WorkspacePaths,
                 config: HubConfig, approvals: ApprovalService, audit: AuditService,
                 checkpoints: CheckpointService, memory=None):
        self.store = store
        self.project = project
        self.paths = paths
        self.config = config
        self.approvals = approvals
        self.audit = audit
        self.checkpoints = checkpoints
        self.memory = memory

    def merge(self, run_id: str, *, approval_id: str = "", by: str = "user",
              commit: bool = False) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise NotFoundError(f"运行不存在: {run_id}")
        if run["status"] not in ("pending_merge", "completed"):
            raise ValidationError(f"运行状态不允许合并: {run['status']}")
        if run.get("acceptance_status") != "approved":
            raise ApprovalRequiredError(
                "验收确认尚未通过，不能合并代码",
                details={"run_id": run_id, "acceptance_status": run.get("acceptance_status")},
            )
        plan = self.store.get_plan(run["plan_id"])
        if not plan:
            raise NotFoundError(f"计划不存在: {run['plan_id']}")
        approval = self.store.get_approval(approval_id) if approval_id else \
            self.approvals.ensure_merge_approval(
                run_id, plan.goal,
                [{"task_id": t.task_id, "patch": t.patch_path} for t in self.store.list_tasks(run_id)
                 if t.patch_path],
                run.get("summary", {}),
            )
        if not approval or approval.status != "approved":
            raise ApprovalRequiredError("合并需要用户批准", details={"approval_id": approval_id})
        tasks = [t for t in self.store.list_tasks(run_id)
                 if t.status == TaskStatus.COMPLETED.value and t.patch_path]
        self._preflight(run, tasks)
        self.store.update_run(run_id, status="merging")
        applied: list[Task] = []
        for task in plan.topological_order():
            matches = [t for t in tasks if t.task_id == task.task_id]
            if not matches:
                continue
            task_row = matches[0]
            patch = task_row.patch_path
            if not Path(patch).exists() or not Path(patch).read_text(encoding="utf-8", errors="replace").strip():
                self.audit.log(by, "merge.skip_empty", subject=task_row.uid)
                continue
            ok, message = apply_patch(self.project.root, patch, three_way=True)
            if not ok:
                for applied_task in applied:
                    self._mark_change(applied_task.uid, ChangeStatus.APPLIED.value)
                self.store.update_run(run_id, status="failed", error=f"{task.task_id} 合并冲突: {message[:500]}")
                self.audit.log(by, "merge.conflict", subject=task_row.uid,
                               payload={"message": message[:1000], "applied": [t.task_id for t in applied]})
                self.checkpoints.create("merge_conflict", run_id=run_id,
                                        extra={"task": task_row.task_id, "message": message})
                return {
                    "status": "conflict", "run_id": run_id, "task_id": task_row.task_id,
                    "message": message, "applied": [t.task_id for t in applied],
                    "hint": "已应用的 Patch 可用 rollback 逐个回滚；冲突需人工处理或重新规划",
                }
            self._mark_change(task_row.uid, ChangeStatus.APPLIED.value)
            applied.append(task_row)
            self.audit.log(by, "merge.applied", subject=task_row.uid,
                           payload={"patch": patch, "files": task_row.spec.scope})
        verification = self._post_merge_verification(applied)
        if any(v["result"] == "failed" for v in verification):
            self.store.update_run(run_id, status="failed", error="合并后验证失败")
            self._refresh_final_summary(
                run, run_id=run_id, applied=applied, verification=verification,
                commit="", status="verification_failed", committed=False,
            )
            self.audit.log(by, "merge.verification_failed", subject=run_id,
                           payload={"verification": verification})
            return {"status": "verification_failed", "run_id": run_id,
                    "verification": verification, "hint": "可执行 rollback 撤销本次合并"}
        commit_hash = ""
        if commit or self.config.auto_merge_commit:
            commit_hash = commit_all(self.project.root, f"agentloop: merge run {run_id}")
        branch = run.get("base_branch") or self.project.branch
        commit_value = commit_hash or (run_git(["rev-parse", "HEAD"], cwd=self.project.root,
                                               check=False).stdout_text or "").strip()  # type: ignore[attr-defined]
        self.store.update_project_git(self.project.project_id, branch=branch, commit=commit_value)
        self.store.update_run(run_id, status="completed", finished_at=now_iso())
        self._refresh_final_summary(
            run, run_id=run_id, applied=applied, verification=verification,
            commit=commit_value, status="completed", committed=bool(commit_hash),
        )
        self.checkpoints.create("after_merge", run_id=run_id,
                                extra={"applied": [t.task_id for t in applied], "commit": commit_hash})
        self.audit.log(by, "merge.completed", subject=run_id,
                       payload={"applied": [t.task_id for t in applied], "commit": commit_hash})
        if self.memory:
            self.memory.sync_workflow(run_id)
            self.memory.sync_task_state(run_id)
        return {"status": "completed", "run_id": run_id, "applied": [t.task_id for t in applied],
                "commit": commit_hash, "verification": verification}

    def _refresh_final_summary(self, run: dict[str, Any], *, run_id: str,
                               applied: list[Task], verification: list[dict[str, Any]],
                               commit: str, status: str, committed: bool) -> None:
        """Replace the pre-merge summary with a post-merge final summary."""
        summary = dict(run.get("summary") or {})
        stale_markers = (
            "主工作区仍", "主工作区当前仍", "尚未实际合并", "尚未应用补丁",
            "仍处于基线", "尚未合并",
        )
        risks = [
            r for r in (summary.get("risks") or [])
            if not any(marker in str(r) for marker in stale_markers)
        ]
        summary["risks"] = risks
        summary["pre_merge_completion"] = summary.get("completion")
        summary["pre_merge_verification"] = summary.get("verification")
        summary["completion"] = "completed" if status == "completed" else "failed"
        passed = [v for v in verification if v.get("result") == "passed"]
        failed = [v for v in verification if v.get("result") == "failed"]
        if status == "completed":
            summary["verification"] = (
                "合并完成；后置验证通过 " + str(len(passed)) + " 项"
                + ("，失败 " + str(len(failed)) + " 项" if failed else "")
            )
        else:
            summary["verification"] = "合并后验证失败：" + "; ".join(
                str(v.get("command", "")) for v in failed
            )
        summary["merge"] = {
            "status": status,
            "applied_tasks": [t.task_id for t in applied],
            "applied_files": sorted({f for t in applied for f in t.spec.scope}),
            "commit": commit,
            "committed": committed,
            "post_merge_verification": verification,
            "merged_at": now_iso(),
        }
        summary["final_status"] = status
        summary["final_commit"] = commit
        summary["final_merge_recommendation"] = "merged" if status == "completed" else status
        summary["merge_recommendation"] = "merged" if status == "completed" else status
        self.store.update_run(run_id, summary=summary)

    def _preflight(self, run: dict[str, Any], tasks: list[Task]) -> None:
        from ..workspace import is_git_repo
        if not is_git_repo(self.project.root):
            raise ValidationError("项目不是 Git 仓库，无法合并 Patch")
        patch_file_set: set[str] = set()
        for task in tasks:
            if task.patch_path and Path(task.patch_path).exists():
                patch_file_set.update(patch_files(task.patch_path))
        dirty = status_porcelain(self.project.root)
        conflicts = [
            d for d in dirty
            if d["path"].replace("\\", "/") in patch_file_set
            or d.get("old_path", "").replace("\\", "/") in patch_file_set
        ]
        if conflicts:
            raise ConflictError(
                "用户工作区中存在与 Patch 重叠的未提交修改，拒绝覆盖",
                details={"files": [d["path"] for d in conflicts[:20]]},
            )
        current_branch = (run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=self.project.root,
                                  check=False).stdout_text or "").strip()  # type: ignore[attr-defined]
        if run.get("base_branch") and current_branch != run["base_branch"]:
            raise ConflictError(f"当前分支 {current_branch} 与计划基线 {run['base_branch']} 不一致")
        # Deterministic cross-task file overlap check. A later round legitimately
        # edits the same file as the round it repairs; allow overlap only when
        # one task is an ancestor of the other, because patches were composed
        # in that order. Unrelated tasks still conflict.
        all_tasks = self.store.list_tasks(run["run_id"])
        deps = {t.task_id: list(t.spec.dependencies) for t in all_tasks}
        patches = []
        for task in tasks:
            if task.patch_path and Path(task.patch_path).exists():
                patches.append((task, set(patch_files(task.patch_path))))
        for i in range(len(patches)):
            for j in range(i + 1, len(patches)):
                overlap = patches[i][1] & patches[j][1]
                if not overlap:
                    continue
                left, right = patches[i][0].task_id, patches[j][0].task_id
                if self._continuation_overlap(deps, left, right):
                    continue
                raise ConflictError(
                    f"任务 {patches[i][0].task_id} 与 {patches[j][0].task_id} 修改了相同文件",
                    details={"files": sorted(overlap)},
                )

    @staticmethod
    def _round_of(task_id: str) -> int:
        import re
        match = re.match(r"^R(\d+)_", str(task_id))
        return int(match.group(1)) if match else 0

    @classmethod
    def _continuation_overlap(cls, deps: dict[str, list[str]],
                              left: str, right: str) -> bool:
        """Allow a later round to edit a file it is repairing.

        Same-round tasks and ordinary plan tasks keep the strict no-overlap
        rule; only an ``R<n>_`` task that depends on an earlier round may
        compose patches on the same file.
        """
        for child, parent in ((left, right), (right, left)):
            child_round = cls._round_of(child)
            if child_round and cls._round_of(parent) != child_round \
                    and cls._depends_on(deps, child, parent):
                return True
        return False

    @staticmethod
    def _depends_on(deps: dict[str, list[str]], task_id: str, target: str) -> bool:
        seen: set[str] = set()
        stack = list(deps.get(task_id, []))
        while stack:
            current = stack.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            stack.extend(deps.get(current, []))
        return False

    def _post_merge_verification(self, tasks: list[Task]) -> list[dict[str, Any]]:
        out = []
        for task in tasks:
            for command in task.spec.verification_commands:
                proc = run_command(command, cwd=self.project.root,
                                   timeout=self.config.verify_timeout_seconds)
                out.append({
                    "task_id": task.task_id, "command": command,
                    "exit_code": proc.exit_code, "result": "passed" if proc.ok else "failed",
                    "output": (proc.stdout + "\n" + proc.stderr)[-1000:],
                })
        return out

    def _mark_change(self, task_uid: str, status: str) -> None:
        changes = self.store.list_changes(task_uid=task_uid)
        for change in changes:
            if change.status != ChangeStatus.REVERTED.value:
                self.store.update_change_status(change.change_id, status,
                                                applied_at=now_iso() if status == "applied" else "")

    def rollback_task(self, task_uid: str, *, by: str = "user") -> dict[str, Any]:
        changes = self.store.list_changes(task_uid=task_uid)
        if not changes:
            raise NotFoundError(f"任务没有变更记录: {task_uid}")
        reverted = []
        for change in changes:
            if change.status == ChangeStatus.APPLIED.value:
                ok, message = reverse_patch(self.project.root, change.patch_path)
                if not ok:
                    raise ConflictError(f"回滚补丁失败: {message}", details={"change_id": change.change_id})
                self.store.update_change_status(change.change_id, ChangeStatus.REVERTED.value)
                reverted.append(change.change_id)
        task = self.store.get_task(task_uid)
        if task and task.workdir:
            try:
                remove_worktree(self.project.root, task.workdir)
            except Exception:
                pass
        self.audit.log(by, "rollback.task", subject=task_uid,
                       payload={"reverted_changes": reverted})
        self.checkpoints.create("rollback", extra={"task_uid": task_uid, "changes": reverted})
        return {"status": "reverted", "task_uid": task_uid, "changes": reverted}

    def rollback_file(self, task_uid: str, path: str, *, by: str = "user") -> dict[str, Any]:
        changes = self.store.list_changes(task_uid=task_uid)
        if not changes:
            raise NotFoundError(f"任务没有变更记录: {task_uid}")
        applied = [c for c in changes if c.status == ChangeStatus.APPLIED.value]
        if not applied:
            raise ConflictError("任务没有已应用的变更可回滚")
        change = applied[0]
        ok, message = reverse_patch(self.project.root, change.patch_path, include=path)
        if not ok:
            raise ConflictError(f"单文件回滚失败: {message}")
        self.audit.log(by, "rollback.file", subject=task_uid, payload={"path": path,
                                                                       "change": change.change_id})
        return {"status": "reverted", "task_uid": task_uid, "file": path,
                "change_id": change.change_id, "message": message}

    def rollback_run(self, run_id: str, *, by: str = "user") -> dict[str, Any]:
        tasks = self.store.list_tasks(run_id)
        reverted = []
        conflicts = []
        for task in reversed(tasks):
            try:
                result = self.rollback_task(task.uid, by=by)
                reverted.extend(result["changes"])
            except (NotFoundError, ConflictError) as exc:
                conflicts.append({"task_uid": task.uid, "error": str(exc)})
        self.audit.log(by, "rollback.run", subject=run_id,
                       payload={"reverted": reverted, "conflicts": conflicts})
        return {"status": "reverted", "run_id": run_id, "changes": reverted, "conflicts": conflicts}
