"""Planning: ask the user-designated main agent for a structured task graph."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..adapters import create_adapter
from ..adapters.base import ExecutionRequest
from ..errors import NotFoundError, ValidationError
from ..models import (
    AgentRecord, Project, Task, TaskPlan, TaskSpec, TaskStatus,
)
from ..storage.repo import Store
from ..util import estimate_tokens, new_id, now_iso, read_json, read_text
from .approvals import ApprovalService
from .audit import AuditService
from .budget import BudgetManager
from .bus import ContextBus
from .checkpoints import CheckpointService
from ..config import HubConfig
from .context import ContextBuilder
from .cost import CostOptimizer
from .governance import GovernanceService
from .memory import MemoryService
from .paths import WorkspacePaths
from .prompts import PromptBuilder


class PlanningService:
    def __init__(self, store: Store, project: Project, paths: WorkspacePaths,
                 config: HubConfig, approvals: ApprovalService, audit: AuditService,
                 budget: BudgetManager, memory: MemoryService, context: ContextBuilder,
                 checkpoints: CheckpointService, bus: ContextBus | None = None,
                 cost: CostOptimizer | None = None,
                 governance: GovernanceService | None = None):
        self.store = store
        self.project = project
        self.paths = paths
        self.config = config
        self.approvals = approvals
        self.audit = audit
        self.budget = budget
        self.memory = memory
        self.context = context
        self.checkpoints = checkpoints
        self.bus = bus
        self.cost = cost
        self.governance = governance
        self.prompts = PromptBuilder(paths, project)

    # -- request ----------------------------------------------------------
    def request_plan(self, goal: str, *, main_agent_id: str = "", model: str = "") -> dict[str, Any]:
        if not goal.strip():
            raise ValidationError("需求目标不能为空")
        main = self._resolve_main(main_agent_id, model)
        plan_id = new_id("plan")
        agents = [self._agent_brief(a) for a in self.store.list_agents()]
        memories = self.memory.injectable()
        conversations = self.store.list_conversations(self.project.project_id, limit=5)
        ctx = self.context.planning_context(
            goal=goal, memories=memories,
            recent_conversations=[
                {"agent_id": c["agent_id"], "title": c["title"], "summary": c.get("summary", {})}
                for c in conversations
            ],
            agents=agents,
        )
        ctx["bootstrap"] = self.memory.bootstrap(agent_id=main.agent_id)
        if self.cost:
            ctx = self.cost.compress_context(
                ctx, self.config.context_summary_tokens, kind="context_compression",
            )
        prompt_path, plan_path = self.prompts.write_plan_request(
            plan_id=plan_id, goal=goal, main_agent=main, model=model, context=ctx,
        )
        if self.bus:
            self.bus.publish("prompt", {
                "purpose": "plan", "plan_id": plan_id, "goal": goal,
                "agent_id": main.agent_id, "model": model,
                "prompt_path": str(prompt_path), "plan_path": str(plan_path),
                "context_tokens": self._estimate(ctx),
                "preview": read_text(prompt_path, default="")[:1200],
            }, source=main.agent_id, correlation_id=plan_id)
        synthetic = self._synthetic_task(
            uid=f"{plan_id}:PLAN", task_id="PLAN", title="需求理解与任务拆分",
            objective=goal, type="analysis", plan_id=plan_id, run_id="",
            workdir=self.project.root, task_dir=str(prompt_path.parent),
            token_budget=self.config.default_plan_token_budget,
        )
        request = ExecutionRequest(
            project_id=self.project.project_id, project_root=self.project.root,
            task=synthetic, workdir=self.project.root, task_dir=str(prompt_path.parent),
            prompt_path=str(prompt_path), result_path=str(plan_path),
            model=model or (self._main_model() or ""),
            timeout_seconds=self.config.plan_timeout_seconds, purpose="plan",
        )
        adapter = create_adapter(main)
        outcome = adapter.execute(request)
        self.budget.record(
            synthetic, main.agent_id, request.model, outcome.usage,
            duration_ms=outcome.duration_ms, run_id="", status=outcome.status,
        )
        self.audit.log(main.agent_id, "plan.requested", subject=plan_id,
                       payload={"goal": goal, "status": outcome.status, "error": outcome.error[:300]})
        if self.bus:
            self.bus.publish("response", {
                "purpose": "plan", "plan_id": plan_id, "agent_id": main.agent_id,
                "status": outcome.status, "error": outcome.error[:500],
                "usage": outcome.usage.to_dict(), "plan_path": str(plan_path),
                "has_plan": (plan_path.exists()),
            }, source=main.agent_id, correlation_id=plan_id)
        if outcome.status in ("waiting_user", "waiting_agent"):
            return {
                "status": "waiting_user", "plan_id": "",
                "prompt_path": str(prompt_path), "plan_path": str(plan_path),
                "agent_id": main.agent_id, "message": outcome.text,
            }
        if outcome.status != "completed":
            return {
                "status": "failed", "plan_id": "", "error": outcome.error,
                "prompt_path": str(prompt_path), "plan_path": str(plan_path),
            }
        data = read_json(plan_path)
        if data is None:
            return {"status": "failed", "plan_id": "", "error": "主节点没有写出计划文件",
                    "prompt_path": str(prompt_path), "plan_path": str(plan_path)}
        try:
            plan = TaskPlan.from_dict(data, project_id=self.project.project_id, plan_id=plan_id)
        except ValidationError as exc:
            repaired = self._repair_plan_with_agent(
                adapter, main, request, synthetic, prompt_path, plan_path, exc.message
            )
            if repaired is None:
                return {"status": "failed", "plan_id": "", "error": f"计划校验失败: {exc.message}",
                        "prompt_path": str(prompt_path), "plan_path": str(plan_path)}
            plan = repaired
        plan.main_agent_id = main.agent_id
        plan.model = request.model
        plan.status = "pending_approval"
        self.store.save_plan(plan)
        architecture = self.governance.ensure_architecture_approval(plan) if self.governance else None
        approval = self.approvals.ensure_plan_approval(plan)
        self.checkpoints.create("plan_generated", extra={"plan_id": plan.plan_id})
        self.audit.log("control-plane", "plan.generated", subject=plan.plan_id,
                       payload={"tasks": len(plan.tasks), "approval_id": approval.approval_id,
                                "architecture_approval": architecture.approval_id if architecture else ""})
        if self.bus:
            self.bus.publish("task_state", {
                "phase": "plan_generated", "plan_id": plan.plan_id,
                "tasks": [{"task_id": t.task_id, "type": t.type, "priority": t.priority}
                          for t in plan.tasks],
                "architecture_required": bool(architecture),
            }, correlation_id=plan.plan_id, sync_level=2)
        return {
            "status": "pending_approval", "plan_id": plan.plan_id,
            "approval_id": approval.approval_id,
            "architecture_approval_id": architecture.approval_id if architecture else "",
            "plan": plan.to_dict(),
            "prompt_path": str(prompt_path), "plan_path": str(plan_path),
        }

    def _repair_plan_with_agent(self, adapter, main, request, synthetic,
                                prompt_path: Path, plan_path: Path, error: str) -> TaskPlan | None:
        """Ask the main agent once to rewrite the invalid plan file."""
        from ..adapters.base import ExecutionRequest
        from ..util import atomic_write_text, read_text

        prompt = read_text(prompt_path)
        atomic_write_text(prompt_path, prompt + (
            "\n\n## 修复要求\n"
            f"上一次输出的 plan.json 校验失败：{error}\n"
            "请基于同一目标重写完整、合法的计划，并覆盖写入：\n"
            f"`{plan_path}`\n"
        ))
        repair_request = ExecutionRequest(
            project_id=request.project_id, project_root=request.project_root,
            task=request.task, workdir=request.workdir, task_dir=request.task_dir,
            prompt_path=request.prompt_path, result_path=request.result_path,
            model=request.model, timeout_seconds=request.timeout_seconds,
            purpose="plan_repair",
        )
        outcome = adapter.execute(repair_request)
        self.budget.record(
            synthetic, main.agent_id, request.model, outcome.usage,
            duration_ms=outcome.duration_ms, run_id="", status=outcome.status,
        )
        self.audit.log(main.agent_id, "plan.repair", payload={
            "status": outcome.status, "error": error[:500],
        })
        if outcome.status != "completed":
            return None
        data = read_json(plan_path)
        if not isinstance(data, dict):
            return None
        try:
            return TaskPlan.from_dict(data, project_id=self.project.project_id,
                                      plan_id=synthetic.plan_id or new_id("plan"))
        except ValidationError:
            return None

    def _resolve_main(self, main_agent_id: str, model: str) -> AgentRecord:
        current = self.store.get_main_node(self.project.project_id)
        agent_id = main_agent_id or (current["agent_id"] if current else "")
        if not agent_id:
            raise ValidationError("尚未指定主节点 Agent（main set）")
        agent = self.store.get_agent(agent_id)
        if not agent:
            raise NotFoundError(f"主节点 Agent 不存在: {agent_id}")
        if not model and current and current["agent_id"] == agent_id:
            model = current["model"]
        if not model:
            model = str((agent.extra or {}).get("selected_model") or "")
        return agent

    def _main_model(self) -> str:
        current = self.store.get_main_node(self.project.project_id)
        if current and current.get("model"):
            return current["model"]
        if current:
            agent = self.store.get_agent(current["agent_id"])
            if agent:
                return str((agent.extra or {}).get("selected_model") or "")
        return ""

    @staticmethod
    def _estimate(value: Any) -> int:
        return estimate_tokens(json.dumps(value, ensure_ascii=False, default=str))

    @staticmethod
    def _agent_brief(agent: AgentRecord) -> dict[str, Any]:
        return {
            "agent_id": agent.agent_id, "name": agent.name, "product": agent.product,
            "edition": agent.edition, "access_level": agent.access_level,
            "adapter_type": agent.adapter_type, "status": agent.status,
            "capabilities": agent.capabilities, "models": agent.models,
        }

    def _synthetic_task(self, *, uid: str, task_id: str, title: str, objective: str,
                        type: str, plan_id: str, run_id: str, workdir: str,
                        task_dir: str, token_budget: int) -> Task:
        spec = TaskSpec(
            task_id=task_id, title=title, objective=objective, type=type,
            token_budget=token_budget, time_limit_minutes=30, retry_limit=0,
        )
        return Task(
            uid=uid, project_id=self.project.project_id, plan_id=plan_id, run_id=run_id,
            spec=spec, status=TaskStatus.RUNNING.value, workdir=workdir, task_dir=task_dir,
            base_commit=self.project.commit,
        )

    # -- import / approve --------------------------------------------------
    def import_plan(self, data: dict[str, Any] | str | Path, *, main_agent_id: str = "",
                    model: str = "") -> dict[str, Any]:
        raw = read_json(data) if isinstance(data, (str, Path)) else data
        if raw is None:
            raise ValidationError(f"无法读取计划文件: {data}")
        plan_id = new_id("plan")
        plan = TaskPlan.from_dict(raw, project_id=self.project.project_id, plan_id=plan_id)
        main = self._resolve_main(main_agent_id, model)
        plan.main_agent_id = main.agent_id
        plan.model = model or self._main_model()
        plan.status = "pending_approval"
        self.store.save_plan(plan)
        architecture = self.governance.ensure_architecture_approval(plan) if self.governance else None
        approval = self.approvals.ensure_plan_approval(plan)
        self.checkpoints.create("plan_generated", extra={"plan_id": plan.plan_id})
        self.audit.log("user", "plan.imported", subject=plan.plan_id,
                       payload={"tasks": len(plan.tasks), "approval_id": approval.approval_id})
        return {"status": "pending_approval", "plan_id": plan.plan_id,
                "approval_id": approval.approval_id,
                "architecture_approval_id": architecture.approval_id if architecture else "",
                "plan": plan.to_dict()}

    def approve_plan(self, plan_id: str, *, approval_id: str = "",
                      note: str = "", by: str = "user") -> dict[str, Any]:
        plan = self.store.get_plan(plan_id)
        if not plan:
            raise NotFoundError(f"计划不存在: {plan_id}")
        if plan.status != "pending_approval":
            raise ValidationError(f"计划状态不是待审批: {plan.status}")
        if self.governance:
            self.governance.require_architecture(plan)
        approval = self.store.get_approval(approval_id) if approval_id else \
            self.approvals.ensure_plan_approval(plan)
        if not approval:
            raise NotFoundError(f"审批不存在: {approval_id}")
        if approval.status == "pending":
            approval = self.approvals.decide(approval.approval_id, approved=True, note=note, by=by)
        if approval.status != "approved":
            self.store.update_plan_status(plan_id, "rejected", approval_id=approval.approval_id)
            self.audit.log(by, "plan.rejected", subject=plan_id, payload={"note": note})
            return {"status": "rejected", "plan_id": plan_id}

        self.store.update_plan_status(plan_id, "approved", approved_at=now_iso(),
                                      approval_id=approval.approval_id)
        base_commit = self.project.commit
        run_id = self.store.create_run(
            self.project.project_id, plan_id, main_agent=plan.main_agent_id,
            model=plan.model, base_commit=base_commit, base_branch=self.project.branch,
            status="ready",
        )
        tasks = self.store.create_tasks(run_id, plan, base_commit)
        self.checkpoints.create("plan_approved", run_id=run_id,
                                extra={"plan_id": plan_id, "tasks": len(tasks)})
        self.audit.log(by, "plan.approved", subject=plan_id,
                       payload={"run_id": run_id, "approval_id": approval.approval_id})
        self.memory.record_decision(
            f"批准计划：{plan.goal}（{len(tasks)} 个任务）", agent_id=plan.main_agent_id,
            model=plan.model, approved=True,
        )
        self.memory.sync_workflow(run_id)
        self.memory.sync_task_state(run_id)
        if self.bus:
            self.bus.publish("workflow", {
                "phase": "plan_approved", "plan_id": plan_id, "run_id": run_id,
                "goal": plan.goal, "tasks": len(tasks),
                "priorities": {t.task_id: t.priority for t in plan.tasks},
            }, source=by, correlation_id=run_id, sync_level=5)
        return {"status": "approved", "plan_id": plan_id, "run_id": run_id,
                "approval_id": approval.approval_id, "tasks": len(tasks)}

    def reject_plan(self, plan_id: str, *, approval_id: str = "", note: str = "",
                    by: str = "user") -> dict[str, Any]:
        plan = self.store.get_plan(plan_id)
        if not plan:
            raise NotFoundError(f"计划不存在: {plan_id}")
        approval = self.store.get_approval(approval_id) if approval_id else \
            self.approvals.ensure_plan_approval(plan)
        if approval and approval.status == "pending":
            self.approvals.decide(approval.approval_id, approved=False, note=note, by=by)
        self.store.update_plan_status(plan_id, "rejected", approval_id=(approval.approval_id if approval else ""))
        self.audit.log(by, "plan.rejected", subject=plan_id, payload={"note": note})
        return {"status": "rejected", "plan_id": plan_id}

    def list_plans(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return [p.to_dict() for p in self.store.list_plans(self.project.project_id, limit)]
