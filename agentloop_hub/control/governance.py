"""Human Governance: architecture, acceptance, merge and release gates.

需求整理-v1 模块 7 的原则是“AI 负责执行，人负责关键决策”。四个关键节点：

1. 架构设计：计划包含 ``architecture`` 任务时，先由用户批准架构方案；
2. 验收确认：运行产出汇总后，用户必须确认成果符合目标；
3. 代码合并：验收通过后，用户再批准把 Patch 写入主工作区；
4. 发布上线：合并完成后，发布动作必须单独获批（本项目只记录门禁，
   不自动执行部署/推送）。
"""

from __future__ import annotations

from typing import Any

from ..errors import ApprovalRequiredError, NotFoundError, ValidationError
from ..models import (
    Approval, ApprovalKind, ApprovalStatus, Project, Task, TaskPlan, TaskStatus,
    TaskType,
)
from ..storage.repo import Store
from ..util import now_iso
from .approvals import ApprovalService
from .audit import AuditService


class GovernanceService:
    def __init__(self, store: Store, project: Project, approvals: ApprovalService,
                 audit: AuditService):
        self.store = store
        self.project = project
        self.approvals = approvals
        self.audit = audit

    # ------------------------------------------------------------------
    # Architecture design gate
    # ------------------------------------------------------------------
    @staticmethod
    def architecture_required(plan: TaskPlan) -> bool:
        for task in plan.tasks:
            if task.type == TaskType.ARCHITECTURE.value:
                return True
            if "architecture" in (task.required_capabilities or []):
                return True
        return False

    def ensure_architecture_approval(self, plan: TaskPlan) -> Approval | None:
        if not self.architecture_required(plan):
            return None
        architecture_tasks = [
            t.to_dict() for t in plan.tasks
            if t.type == TaskType.ARCHITECTURE.value or "architecture" in (t.required_capabilities or [])
        ]
        existing = self._find(plan_id=plan.plan_id, kind=ApprovalKind.ARCHITECTURE.value)
        if existing and existing.status in (ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value):
            return existing
        approval = self.approvals.create(
            ApprovalKind.ARCHITECTURE.value,
            subject=f"架构设计审批：{plan.goal[:120]}",
            details={
                "what": "确认架构方案、模块边界、接口与关键取舍",
                "why": "架构设计属于人类关键决策，批准后才允许进入任务分发",
                "goal": plan.goal,
                "constraints": plan.constraints,
                "architecture_tasks": architecture_tasks,
                "risks": plan.risks,
                "alternatives": list((plan.raw or {}).get("alternatives", []) or []),
                "rollback": "拒绝后修改计划或重新规划，不产生代码变更",
                "recommendation": "review",
            },
            plan_id=plan.plan_id,
        )
        self.audit.log("control-plane", "governance.architecture_requested",
                       subject=plan.plan_id, payload={"approval_id": approval.approval_id})
        return approval

    def architecture_approved(self, plan_id: str) -> bool:
        plan = self.store.get_plan(plan_id)
        if not plan or not self.architecture_required(plan):
            return True
        approval = self._find(plan_id=plan_id, kind=ApprovalKind.ARCHITECTURE.value)
        return bool(approval and approval.status == ApprovalStatus.APPROVED.value)

    def require_architecture(self, plan: TaskPlan) -> None:
        if not self.architecture_approved(plan.plan_id):
            approval = self._find(plan_id=plan.plan_id, kind=ApprovalKind.ARCHITECTURE.value)
            raise ApprovalRequiredError(
                "计划包含架构设计，需先通过架构审批",
                details={"plan_id": plan.plan_id,
                         "approval_id": approval.approval_id if approval else ""},
            )

    # ------------------------------------------------------------------
    # Acceptance gate
    # ------------------------------------------------------------------
    def acceptance_approval(self, run_id: str) -> Approval | None:
        return self._find(run_id=run_id, kind=ApprovalKind.ACCEPTANCE.value)

    def ensure_acceptance_approval(self, run_id: str, plan: TaskPlan, summary: dict[str, Any],
                                   tasks: list[Task]) -> Approval:
        existing = self._find(run_id=run_id, kind=ApprovalKind.ACCEPTANCE.value)
        if existing and existing.status in (ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value):
            return existing
        task_rows = [
            {
                "task_id": t.task_id, "title": t.spec.title, "status": t.status,
                "agent": t.assigned_agent, "verification": t.verification_status,
                "files_changed": (self.store.get_result(t.uid) or {}).get("payload", {}).get("files_changed", []),
            }
            for t in tasks
        ]
        approval = self.approvals.create(
            ApprovalKind.ACCEPTANCE.value,
            subject=f"验收确认：{plan.goal[:120]}",
            details={
                "what": "确认所有任务成果、验证结论与未解决项符合用户目标",
                "why": "验收确认必须由用户做出，执行 Agent 不能自证完成",
                "run_id": run_id, "goal": plan.goal,
                "completion": summary.get("completion", ""),
                "changes": summary.get("changes", []),
                "verification": summary.get("verification", ""),
                "risks": summary.get("risks", []),
                "tasks": task_rows,
                "rollback": "拒绝则运行标记失败，可返工或重新规划",
                "recommendation": "approve" if summary.get("merge_recommendation") == "approve" else "reject",
            },
            run_id=run_id,
        )
        self.audit.log("control-plane", "governance.acceptance_requested",
                       subject=run_id, payload={"approval_id": approval.approval_id})
        return approval

    def accept_run(self, run_id: str, *, approval_id: str, by: str = "user") -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise NotFoundError(f"运行不存在: {run_id}")
        if run.get("acceptance_status") == ApprovalStatus.APPROVED.value:
            return self._pending_merge_outcome(run_id)
        approval = self.store.get_approval(approval_id)
        if not approval or approval.status != ApprovalStatus.APPROVED.value:
            raise ApprovalRequiredError("验收确认需要用户批准", details={"approval_id": approval_id})
        plan = self.store.get_plan(run["plan_id"])
        if not plan:
            raise NotFoundError(f"计划不存在: {run['plan_id']}")
        tasks = self.store.list_tasks(run_id)
        patches = [
            {"task_id": t.task_id, "agent": t.assigned_agent, "patch": t.patch_path,
             "verification": t.verification_status}
            for t in tasks if t.patch_path
        ]
        merge = self.approvals.ensure_merge_approval(
            run_id, plan.goal, patches, run.get("summary", {}) or {},
        )
        self.store.update_run(
            run_id, status="pending_merge",
            acceptance_status=ApprovalStatus.APPROVED.value,
            acceptance_at=now_iso(),
        )
        self.audit.log(by, "governance.accepted", subject=run_id,
                       payload={"approval_id": approval_id, "merge_approval": merge.approval_id})
        return {
            "status": "pending_merge", "run_id": run_id,
            "acceptance_status": "approved", "approval_id": merge.approval_id,
        }

    def reject_run(self, run_id: str, *, approval_id: str, by: str = "user") -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise NotFoundError(f"运行不存在: {run_id}")
        self.store.update_run(
            run_id, status="failed", acceptance_status=ApprovalStatus.REJECTED.value,
            error="用户拒绝验收",
        )
        self.audit.log(by, "governance.acceptance_rejected", subject=run_id,
                       payload={"approval_id": approval_id})
        return {"status": "rejected", "run_id": run_id, "acceptance_status": "rejected"}

    def _pending_merge_outcome(self, run_id: str) -> dict[str, Any]:
        merge = self._find(run_id=run_id, kind=ApprovalKind.MERGE.value)
        return {"status": "pending_merge", "run_id": run_id,
                "acceptance_status": "approved",
                "approval_id": merge.approval_id if merge else ""}

    # ------------------------------------------------------------------
    # Release gate
    # ------------------------------------------------------------------
    def release_approval(self, run_id: str) -> Approval | None:
        return self._find(run_id=run_id, kind=ApprovalKind.RELEASE.value)

    def ensure_release_approval(self, run_id: str) -> Approval:
        run = self.store.get_run(run_id)
        if not run:
            raise NotFoundError(f"运行不存在: {run_id}")
        if run["status"] not in ("completed", "released"):
            raise ValidationError(f"只有已完成（合并后）的运行才能申请发布: {run['status']}")
        if run.get("acceptance_status") != ApprovalStatus.APPROVED.value:
            raise ApprovalRequiredError("验收未通过，不能申请发布")
        existing = self.release_approval(run_id)
        if existing and existing.status in (ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value):
            return existing
        plan = self.store.get_plan(run["plan_id"])
        approval = self.approvals.create(
            ApprovalKind.RELEASE.value,
            subject=f"发布审批：{(plan.goal if plan else run_id)[:120]}",
            details={
                "what": "确认把本次合并结果发布到目标环境（本项目只记录门禁，不自动部署）",
                "why": "发布上线属于人类关键决策",
                "run_id": run_id, "commit": run.get("base_commit", ""),
                "summary": run.get("summary", {}),
                "rollback": "按任务/文件回滚，或重新部署上一版本",
                "recommendation": "approve",
            },
            run_id=run_id,
        )
        self.audit.log("control-plane", "governance.release_requested",
                       subject=run_id, payload={"approval_id": approval.approval_id})
        return approval

    def release_run(self, run_id: str, *, approval_id: str, by: str = "user") -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise NotFoundError(f"运行不存在: {run_id}")
        approval = self.store.get_approval(approval_id)
        if not approval or approval.status != ApprovalStatus.APPROVED.value:
            raise ApprovalRequiredError("发布需要用户批准", details={"approval_id": approval_id})
        release_at = now_iso()
        summary = dict(run.get("summary") or {})
        summary["final_status"] = "released"
        summary["release"] = {
            "status": ApprovalStatus.APPROVED.value,
            "approval_id": approval_id,
            "released_at": release_at,
        }
        self.store.update_run(
            run_id, status="released", release_status=ApprovalStatus.APPROVED.value,
            release_at=release_at, summary=summary,
        )
        self.audit.log(by, "governance.released", subject=run_id,
                       payload={"approval_id": approval_id})
        return {"status": "released", "run_id": run_id,
                "release_status": "approved", "approval_id": approval_id}

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def status(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise NotFoundError(f"运行不存在: {run_id}")
        approvals = [
            a.to_dict() for a in self.store.list_approvals(self.project.project_id, limit=500)
            if a.run_id == run_id and a.kind in {
                ApprovalKind.ACCEPTANCE.value, ApprovalKind.MERGE.value,
                ApprovalKind.RELEASE.value,
            }
        ]
        return {
            "run_id": run_id,
            "status": run["status"],
            "acceptance_status": run.get("acceptance_status", "pending"),
            "release_status": run.get("release_status", "not_requested"),
            "acceptance_at": run.get("acceptance_at", ""),
            "release_at": run.get("release_at", ""),
            "approvals": approvals,
        }

    def _find(self, *, plan_id: str = "", run_id: str = "", kind: str) -> Approval | None:
        approvals = self.store.list_approvals(self.project.project_id, limit=500)
        for approval in approvals:
            if approval.kind != kind:
                continue
            if plan_id and approval.plan_id != plan_id:
                continue
            if run_id and approval.run_id != run_id:
                continue
            if approval.status in (ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value):
                return approval
        return None


__all__ = ["GovernanceService"]
