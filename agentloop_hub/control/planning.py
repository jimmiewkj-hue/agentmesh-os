"""Planning: ask the user-designated main agent for a structured task graph."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from ..adapters import create_adapter
from ..adapters.base import ExecutionRequest
from ..errors import ConflictError, NotFoundError, ValidationError
from ..workspace import create_worktree
from ..models import (
    AgentRecord, Project, Task, TaskPlan, TaskSpec, TaskStatus,
)
from ..storage.repo import Store
from ..util import atomic_write_text, estimate_tokens, new_id, now_iso, read_json, read_text, write_json
from .approvals import ApprovalService
from .audit import AuditService
from .budget import BudgetManager
from .bus import ContextBus
from .checkpoints import CheckpointService
from ..config import HubConfig
from .collaboration import (
    build_contract, enforce_plan_contract, normalize_plan_payload,
)
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
    def request_plan(self, goal: str, *, main_agent_id: str = "", model: str = "",
                     persist: bool = True, plan_id: str = "",
                     round_context: dict[str, Any] | None = None) -> dict[str, Any]:
        if not goal.strip():
            raise ValidationError("需求目标不能为空")
        main = self._resolve_main(main_agent_id, model)
        plan_id = plan_id or new_id("plan")
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
        contract = build_contract(self.store, self.project, main.agent_id,
                                  model or self._main_model())
        ctx["collaboration"] = contract
        if round_context:
            ctx["round_context"] = round_context
        if self.cost:
            ctx = self.cost.compress_context(
                ctx, self.config.context_summary_tokens, kind="context_compression",
            )
        workspace = self._plan_workspace(plan_id)
        canonical_plan_path = self.paths.planning / plan_id / "plan.json"
        gui_mode = str(main.adapter_type) == "gui_automation"
        agent_plan_path = (Path(workspace) / "plan.json") if gui_mode else canonical_plan_path
        prompt_path, plan_path = self.prompts.write_plan_request(
            plan_id=plan_id, goal=goal, main_agent=main, model=model, context=ctx,
            output_path=agent_plan_path, workspace=workspace, gui_mode=gui_mode,
            round_context=round_context,
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
            workdir=workspace, task_dir=str(prompt_path.parent),
            token_budget=self.config.default_plan_token_budget,
        )
        attempt_id = new_id("att")
        request_id = uuid.uuid4().hex
        request_marker = f"AGENTMESH_GUI_MARKER_PLAN_{request_id}"
        request = ExecutionRequest(
            project_id=self.project.project_id, project_root=self.project.root,
            task=synthetic, workdir=workspace, task_dir=str(prompt_path.parent),
            prompt_path=str(prompt_path), result_path=str(agent_plan_path),
            attempt_id=attempt_id, request_id=request_id,
            metadata={"request_marker": request_marker},
            model=model or (self._main_model() or ""),
            timeout_seconds=self.config.plan_timeout_seconds, purpose="plan",
            full_control=bool(self.config.full_control),
        )
        self.store.create_attempt(
            attempt_id=attempt_id, project_id=self.project.project_id,
            task_uid=synthetic.uid, run_id="", attempt_number=1,
            request_id=request_id, purpose="plan", workdir=str(workspace),
            result_path=str(agent_plan_path),
            binding={"marker": request_marker, "phase": "plan"},
            input_snapshot={"goal": goal, "plan_id": plan_id},
        )
        self.audit.log("control-plane", "workspace.created", subject=plan_id,
                       payload={"expected": workspace, "kind": "plan",
                                "base_commit": self.project.commit})
        activity_path = self.paths.planning / plan_id / "activity.json"
        activity = {
            "phase": "round_plan" if round_context else "plan",
            "round": int((round_context or {}).get("round") or 0),
            "plan_id": plan_id, "agent_id": main.agent_id, "agent_name": main.name,
            "model": request.model, "status": "running",
            "started_at": now_iso(), "prompt_path": str(prompt_path),
            "workspace": workspace,
        }
        write_json(activity_path, activity)
        adapter = create_adapter(main)
        outcome = adapter.execute(request)
        self.store.update_attempt(
            attempt_id, status=outcome.status, session_id=outcome.session_id,
            result_path=outcome.result_path or str(agent_plan_path),
            binding={"marker": request_marker, "phase": "plan",
                     "submission": outcome.submission_receipt,
                     "model_evidence": (outcome.extras or {}).get(
                         "model_evidence", {})},
            error=outcome.error[:500], finished_at=now_iso(),
        )
        activity.update({
            "status": outcome.status, "error": outcome.error[:500],
            "finished_at": now_iso(), "duration_ms": outcome.duration_ms,
        })
        write_json(activity_path, activity)
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
        data = None
        if outcome.status != "completed":
            # A GUI agent can finish the plan and write plan.json while the
            # post-reply interface check fails (e.g. the app appended an
            # effort suffix to the visible model).  Salvage a valid plan
            # instead of discarding minutes of completed work.
            if outcome.status == "failed":
                data = read_json(agent_plan_path)
                if isinstance(data, dict) and data:
                    write_json(plan_path, data)
                    activity.update({
                        "status": "salvaged",
                        "salvaged": True,
                        "original_error": outcome.error[:500],
                        "finished_at": now_iso(),
                    })
                    write_json(activity_path, activity)
                    self.audit.log(main.agent_id, "plan.salvaged", subject=plan_id,
                                   payload={"error": outcome.error[:500],
                                            "plan_path": str(plan_path)})
            if not isinstance(data, dict) or not data:
                return {
                    "status": "failed", "plan_id": "", "error": outcome.error,
                    "prompt_path": str(prompt_path), "plan_path": str(plan_path),
                }
        if data is None:
            data = read_json(agent_plan_path)
        if not isinstance(data, dict):
            data = outcome.result_payload if isinstance(outcome.result_payload, dict) else None
        if isinstance(data, dict):
            write_json(plan_path, data)
        else:
            return {"status": "failed", "plan_id": "", "error": "主节点没有写出计划文件",
                    "prompt_path": str(prompt_path), "plan_path": str(plan_path)}
        try:
            plan = TaskPlan.from_dict(normalize_plan_payload(data),
                                      project_id=self.project.project_id, plan_id=plan_id)
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
        contract_notes = enforce_plan_contract(plan, contract)
        plan.validate()
        if not persist:
            plan.status = "draft"
            self.audit.log("control-plane", "plan.round_delta_generated",
                           subject=plan_id,
                           payload={"tasks": [t.task_id for t in plan.tasks],
                                    "round": (round_context or {}).get("round", 0),
                                    "contract_notes": contract_notes})
            return {
                "status": "completed", "plan_id": plan_id,
                "plan": plan.to_dict(), "tasks": [t.to_dict() for t in plan.tasks],
                "contract_notes": contract_notes,
                "prompt_path": str(prompt_path), "plan_path": str(plan_path),
            }
        plan.status = "pending_approval"
        self.store.save_plan(plan)
        self.audit.log("control-plane", "plan.collaboration_enforced",
                       subject=plan.plan_id,
                       payload={"contract": contract, "notes": contract_notes})
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

    def request_round_plan(self, *, goal: str, base_plan: TaskPlan, round_number: int,
                           round_context: dict[str, Any]) -> dict[str, Any]:
        """Ask the main node for the next minimal round without persisting a plan."""
        result = self.request_plan(
            goal,
            main_agent_id=base_plan.main_agent_id,
            model=base_plan.model,
            persist=False,
            plan_id=new_id(f"round{round_number}"),
            round_context=round_context,
        )
        if result.get("status") != "completed":
            return result
        try:
            payload = dict(result["plan"])
            for internal in ("revision", "content_hash", "status", "created_at"):
                payload.pop(internal, None)
            plan = TaskPlan.from_dict(
                payload, project_id=self.project.project_id,
                plan_id=str(result["plan_id"]),
            )
        except ValidationError as exc:
            return {"status": "failed", "error": f"增量计划校验失败: {exc.message}",
                    "prompt_path": result.get("prompt_path", "")}
        return {
            "status": "completed", "plan": plan,
            "tasks": [t.to_dict() for t in plan.tasks],
            "contract_notes": result.get("contract_notes", []),
            "prompt_path": result.get("prompt_path", ""),
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
            purpose="plan_repair", full_control=bool(self.config.full_control),
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
        data = read_json(request.result_path)
        if not isinstance(data, dict) and isinstance(outcome.result_payload, dict):
            data = outcome.result_payload
        if not isinstance(data, dict):
            return None
        if isinstance(data, dict):
            write_json(plan_path, data)
        try:
            return TaskPlan.from_dict(normalize_plan_payload(data),
                                      project_id=self.project.project_id,
                                      plan_id=synthetic.plan_id or new_id("plan"))
        except ValidationError:
            return None

    def _plan_workspace(self, plan_id: str) -> str:
        target = self.paths.plan_workspace(plan_id)
        try:
            create_worktree(self.project.root, target, self.project.commit)
        except Exception as exc:
            self.audit.log("control-plane", "workspace.create_failed", subject=plan_id,
                           payload={"expected": str(target), "error": str(exc)[:300]})
            return str(self.project.root)
        return str(target)

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
        plan = TaskPlan.from_dict(normalize_plan_payload(raw),
                                  project_id=self.project.project_id, plan_id=plan_id)
        main = self._resolve_main(main_agent_id, model)
        plan.main_agent_id = main.agent_id
        plan.model = model or self._main_model()
        contract = build_contract(self.store, self.project, main.agent_id, plan.model)
        contract_notes = enforce_plan_contract(plan, contract)
        plan.validate()
        plan.status = "pending_approval"
        self.store.save_plan(plan)
        architecture = self.governance.ensure_architecture_approval(plan) if self.governance else None
        approval = self.approvals.ensure_plan_approval(plan)
        self.checkpoints.create("plan_generated", extra={"plan_id": plan.plan_id})
        self.audit.log("user", "plan.imported", subject=plan.plan_id,
                       payload={"tasks": len(plan.tasks), "approval_id": approval.approval_id})
        self.audit.log("control-plane", "plan.collaboration_enforced",
                       subject=plan.plan_id,
                       payload={"contract": contract, "notes": contract_notes})
        return {"status": "pending_approval", "plan_id": plan.plan_id,
                "approval_id": approval.approval_id,
                "architecture_approval_id": architecture.approval_id if architecture else "",
                "plan": plan.to_dict()}

    def approve_plan(self, plan_id: str, *, approval_id: str = "",
                      note: str = "", by: str = "user") -> dict[str, Any]:
        plan = self.store.get_plan(plan_id)
        if not plan:
            raise NotFoundError(f"计划不存在: {plan_id}")
        approval = self.store.get_approval(approval_id) if approval_id else \
            self.approvals.ensure_plan_approval(plan)
        if not approval:
            raise NotFoundError(f"审批不存在: {approval_id}")
        if approval.project_id != self.project.project_id \
                or approval.kind != "plan" or approval.plan_id != plan_id:
            raise ConflictError(
                "审批票据不属于该计划",
                details={"approval_id": approval.approval_id,
                         "kind": approval.kind, "plan_id": approval.plan_id},
            )
        if approval.status == "approved":
            existing = self.store.get_run_by_approval(approval.approval_id)
            if existing:
                return {"status": "approved", "plan_id": plan_id,
                        "run_id": existing["run_id"],
                        "approval_id": approval.approval_id,
                        "tasks": len(self.store.list_tasks(existing["run_id"])),
                        "idempotent": True}
            raise ConflictError("审批已通过但对应运行缺失，拒绝再次创建运行")
        if approval.status != "pending":
            raise ConflictError(f"审批已失效或被拒绝: {approval.approval_id} "
                                f"({approval.status})")
        if plan.status != "pending_approval":
            raise ConflictError(f"计划状态不是待审批: {plan.status}")
        details = approval.details or {}
        if str(details.get("plan_hash") or "") != str(plan.content_hash or ""):
            raise ConflictError(
                "审批票据绑定的内容哈希与当前计划不一致，请重新审批",
                details={"ticket_hash": details.get("plan_hash"),
                         "plan_hash": plan.content_hash},
            )
        if int(details.get("plan_revision") or 0) != int(plan.revision or 1):
            raise ConflictError(
                "审批票据绑定的修订号与当前计划不一致，请重新审批",
                details={"ticket_revision": details.get("plan_revision"),
                         "plan_revision": plan.revision},
            )
        if self.governance:
            self.governance.require_architecture(plan)

        base_commit = self.project.commit
        run_id, task_count = self.store.approve_plan_atomic(
            approval_id=approval.approval_id, plan_id=plan_id,
            project_id=self.project.project_id,
            expected_hash=plan.content_hash,
            expected_revision=int(plan.revision or 1),
            base_commit=base_commit, base_branch=self.project.branch,
            max_rounds=int(self.config.max_rounds), by=by, note=note,
        )
        if not task_count:
            task_count = len(self.store.list_tasks(run_id))
        self.checkpoints.create("plan_approved", run_id=run_id,
                                extra={"plan_id": plan_id, "tasks": task_count})
        self.audit.log(by, "plan.approved", subject=plan_id,
                       payload={"run_id": run_id, "approval_id": approval.approval_id,
                                "plan_revision": plan.revision,
                                "plan_hash": plan.content_hash})
        self.memory.record_decision(
            f"批准计划：{plan.goal}（{task_count} 个任务）", agent_id=plan.main_agent_id,
            model=plan.model, approved=True,
        )
        self.memory.sync_workflow(run_id)
        self.memory.sync_task_state(run_id)
        if self.bus:
            self.bus.publish("workflow", {
                "phase": "plan_approved", "plan_id": plan_id, "run_id": run_id,
                "goal": plan.goal, "tasks": task_count,
                "priorities": {t.task_id: t.priority for t in plan.tasks},
            }, source=by, correlation_id=run_id, sync_level=5)
        return {"status": "approved", "plan_id": plan_id, "run_id": run_id,
                "approval_id": approval.approval_id, "tasks": task_count}

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
