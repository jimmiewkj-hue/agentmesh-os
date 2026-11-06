"""Human governance: plan, merge, high-risk and budget approvals."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from ..config import HubConfig
from ..models import (
    Approval, ApprovalKind, ApprovalStatus, Project, RiskLevel, Task, TaskPlan,
    pattern_overlap,
)
from ..storage.repo import Store
from ..util import new_id, now_iso


class ApprovalService:
    def __init__(self, store: Store, project: Project, config: HubConfig):
        self.store = store
        self.project = project
        self.config = config

    # -- generic ----------------------------------------------------------
    def create(self, kind: str, subject: str, details: dict[str, Any], *,
               plan_id: str = "", run_id: str = "", task_uid: str = "") -> Approval:
        existing = self.store.find_pending_approval(
            self.project.project_id, kind=kind, subject=subject, plan_id=plan_id,
            run_id=run_id,
        )
        if existing and (not task_uid or existing.task_uid == task_uid):
            # A pending plan ticket for older content must not be reused for a
            # new revision; expire it and create a ticket bound to the new hash.
            if kind == ApprovalKind.PLAN.value and \
                    str(existing.details.get("plan_hash") or "") != \
                    str(details.get("plan_hash") or ""):
                self.store.expire_approval(existing.approval_id)
            else:
                return existing
        approval = Approval(
            approval_id=new_id("apr"), project_id=self.project.project_id, kind=kind,
            subject=subject, details=details, status=ApprovalStatus.PENDING.value,
            plan_id=plan_id, run_id=run_id, task_uid=task_uid, requested_at=now_iso(),
        )
        self.store.create_approval(approval)
        self.store.append_audit(self.project.project_id, "control-plane", "approval.requested",
                                subject=approval.approval_id,
                                payload={"kind": kind, "subject": subject, "details": details})
        return approval

    def pending(self, *, limit: int = 100) -> list[Approval]:
        return self.store.list_approvals(self.project.project_id, status="pending", limit=limit)

    def all(self, *, limit: int = 100) -> list[Approval]:
        return self.store.list_approvals(self.project.project_id, limit=limit)

    def decide(self, approval_id: str, *, approved: bool, note: str = "", by: str = "user") -> Approval:
        approval = self.store.get_approval(approval_id)
        if not approval:
            from ..errors import NotFoundError
            raise NotFoundError(f"审批不存在: {approval_id}")
        if approval.status != ApprovalStatus.PENDING.value:
            from ..errors import ConflictError
            raise ConflictError(f"审批已处理: {approval_id} ({approval.status})")
        result = self.store.decide_approval(
            approval_id, "approved" if approved else "rejected", by=by, note=note
        )
        self.store.append_audit(self.project.project_id, by, "approval.decided",
                                subject=approval_id,
                                payload={"approved": approved, "note": note, "kind": approval.kind})
        return result

    def is_approved(self, approval_id: str) -> bool:
        approval = self.store.get_approval(approval_id)
        return bool(approval and approval.status == ApprovalStatus.APPROVED.value)

    # -- risk detection ----------------------------------------------------
    def high_risk_reasons(self, task: Task) -> list[str]:
        reasons: list[str] = []
        if task.spec.risk_level in (RiskLevel.HIGH.value, RiskLevel.CRITICAL.value):
            reasons.append(f"任务声明风险等级为 {task.spec.risk_level}")
        for command in task.spec.verification_commands:
            for pattern in self.config.dangerous_command_patterns:
                if re.search(pattern, command, re.I):
                    reasons.append(f"验证命令命中高风险模式 /{pattern}/: {command}")
                    break
        for pattern in self.config.high_risk_file_patterns:
            risky_name = Path(pattern).name
            if pattern_overlap(task.spec.scope, [pattern]) or any(
                fnmatch.fnmatch(s.replace("\\", "/"), pattern)
                or fnmatch.fnmatch(Path(s.replace("\\", "/")).name, risky_name)
                for s in task.spec.scope if not any(ch in s for ch in "*?[")
            ):
                reasons.append(f"修改范围涉及高风险文件模式 {pattern}")
        for scope in task.spec.scope:
            normalized = scope.replace("\\", "/")
            if normalized.startswith("../") or normalized.startswith("/") or ":" in normalized[:3]:
                reasons.append(f"修改范围在项目目录之外或为绝对路径: {scope}")
        if task.spec.approval_policy == "required":
            reasons.append("任务要求执行前人工审批")
        return sorted(set(reasons))

    def ensure_plan_approval(self, plan: TaskPlan) -> Approval:
        risk_summary = []
        for spec in plan.tasks:
            if spec.risk_level in (RiskLevel.HIGH.value, RiskLevel.CRITICAL.value):
                risk_summary.append(f"{spec.task_id}: {spec.risk_level}")
        return self.create(
            ApprovalKind.PLAN.value,
            subject=f"计划审批：{plan.goal[:120]}",
            details={
                "goal": plan.goal,
                "constraints": plan.constraints,
                "tasks": [t.to_dict() for t in plan.tasks],
                "high_risk_tasks": risk_summary,
                "main_agent": plan.main_agent_id,
                "model": plan.model,
                "plan_hash": plan.content_hash,
                "plan_revision": int(plan.revision),
                "project_id": self.project.project_id,
                "approval_kind": ApprovalKind.PLAN.value,
                "estimated_tokens": sum(int(t.token_budget or 0) for t in plan.tasks),
                "what": "批准后才会分发子任务",
                "why": "用户主权原则：计划须经用户批准",
                "rollback": "计划未执行前可直接拒绝",
                "recommendation": "approve",
            },
            plan_id=plan.plan_id,
        )

    def ensure_high_risk_approval(self, task: Task, agent_name: str, reasons: list[str],
                                  *, budget_exceeded: bool = False) -> Approval | None:
        if not reasons:
            return None
        # If the user already approved this exact task, do not ask again on a
        # resume or retry. A pending request still blocks.
        existing = [
            a for a in self.store.list_approvals(self.project.project_id, limit=500)
            if a.task_uid == task.uid and a.kind == ApprovalKind.HIGH_RISK.value
        ]
        approved = [a for a in existing if a.status == ApprovalStatus.APPROVED.value]
        pending = [a for a in existing if a.status == ApprovalStatus.PENDING.value]
        if pending:
            return pending[0]
        if approved and not budget_exceeded:
            return approved[0]
        return self.create(
            ApprovalKind.HIGH_RISK.value,
            subject=f"{task.task_id} 高风险操作审批",
            details={
                "what": task.spec.objective,
                "why": "；".join(reasons),
                "agent": agent_name,
                "task_id": task.task_id,
                "files": task.spec.scope,
                "risk_level": task.spec.risk_level,
                "rollback": "任务在独立 Worktree 中执行，可丢弃或回滚",
                "estimated_tokens": task.spec.token_budget,
                "budget_exceeded": budget_exceeded,
                "recommendation": "reject" if budget_exceeded else "review",
            },
            run_id=task.run_id,
            task_uid=task.uid,
        )

    def ensure_merge_approval(self, run_id: str, plan_goal: str, patches: list[dict[str, Any]],
                              summary: dict[str, Any]) -> Approval:
        return self.create(
            ApprovalKind.MERGE.value,
            subject=f"合并审批：{plan_goal[:120]}",
            details={
                "run_id": run_id,
                "patches": patches,
                "summary": summary,
                "what": "将各任务 Patch 依次应用到当前工作区",
                "why": "所有成果须经用户批准后合并",
                "rollback": "合并后可按任务/文件回滚",
                "recommendation": "approve" if not summary.get("blocked") else "reject",
            },
            run_id=run_id,
        )

    def ensure_budget_approval(self, task: Task, used: int, budget: int) -> Approval:
        existing = [
            a for a in self.store.list_approvals(self.project.project_id, limit=500)
            if a.task_uid == task.uid and a.kind == ApprovalKind.BUDGET.value
        ]
        pending = [a for a in existing if a.status == ApprovalStatus.PENDING.value]
        approved = [a for a in existing if a.status == ApprovalStatus.APPROVED.value]
        if pending:
            return pending[0]
        if approved:
            return approved[0]
        return self.create(
            ApprovalKind.BUDGET.value,
            subject=f"{task.task_id} 预算超限",
            details={
                "what": f"任务 {task.task_id} 已消耗 {used} tokens，超过预算 {budget}",
                "why": "防止无效重试继续消耗预算",
                "agent": task.assigned_agent,
                "rollback": "可取消任务或调整预算后继续",
                "recommendation": "reject",
            },
            run_id=task.run_id,
            task_uid=task.uid,
        )
