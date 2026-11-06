"""Task execution loop, verification orchestration, recovery and summary."""

from __future__ import annotations

import json
import shutil
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from ..adapters import create_adapter
from ..adapters.base import ExecutionRequest
from ..config import HubConfig
from ..errors import ConflictError, NotFoundError, ValidationError
from ..models import (
    AgentRecord, ChangeRecord, ChangeStatus, Project, ResultPackage, Task, TaskPlan,
    TaskSpec, TaskStatus, VerificationReport, VerificationStatus, scope_matches,
)
from ..storage.repo import Store
from ..util import (
    atomic_write_text, ensure_dir, estimate_tokens, new_id, now_iso, read_json, read_text,
    sha1_hex, stable_json, tree_hash, write_json,
)
from ..workspace import (
    LeaseManager, apply_patch, create_worktree, generate_patch, patch_files, run_git,
)
from .approvals import ApprovalService
from .audit import AuditService
from .budget import BudgetManager
from .bus import ContextBus
from .checkpoints import CheckpointService
from .context import ContextBuilder
from .cost import CostOptimizer
from .governance import GovernanceService
from .memory import MemoryService
from .model_profile import resolve_profile
from .paths import WorkspacePaths
from .prompts import PromptBuilder
from .router import CapabilityRouter
from .roles import RolePolicy, classify_model
from .rounds import (
    RoundDecision, collect_unresolved, decide as decide_round, fallback_specs,
    prefix_delta_specs, round_tasks_for, signature_of,
)
from .scheduler import TaskScheduler
from .verification import VerificationOutcome, Verifier


TERMINAL = {
    TaskStatus.COMPLETED.value, TaskStatus.FAILED.value,
    TaskStatus.REJECTED.value, TaskStatus.CANCELED.value,
}


class RunService:
    def __init__(self, store: Store, project: Project, paths: WorkspacePaths,
                 config: HubConfig, approvals: ApprovalService, audit: AuditService,
                 budget: BudgetManager, memory: MemoryService, context: ContextBuilder,
                 checkpoints: CheckpointService, verifier: Verifier,
                 bus: ContextBus | None = None, router: CapabilityRouter | None = None,
                 cost: CostOptimizer | None = None,
                 governance: GovernanceService | None = None,
                 roles: RolePolicy | None = None,
                 planner: Any | None = None):
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
        self.verifier = verifier
        self.bus = bus
        self.router = router
        self.cost = cost
        self.governance = governance
        self.roles = roles
        self.planner = planner
        self.scheduler = TaskScheduler(config)
        self.prompts = PromptBuilder(paths, project)
        self.leases = LeaseManager(store)
        self._wt_lock = threading.Lock()
        self._run_locks: dict[str, threading.Lock] = {}
        self._agent_locks: dict[str, threading.Lock] = {}
        self._agent_locks_guard = threading.Lock()
        self._futures: dict[str, Future] = {}
        self._active_run_id = ""
        self._active_tasks: set[str] = set()
        self._active_tasks_lock = threading.Lock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancel_lock = threading.Lock()

    def _agent_lock(self, agent_id: str) -> threading.Lock:
        with self._agent_locks_guard:
            return self._agent_locks.setdefault(agent_id, threading.Lock())

    # ------------------------------------------------------------------
    # Adapter notifications: block the run, then resume automatically
    # ------------------------------------------------------------------
    def _on_adapter_notification(self, task: Task, agent: AgentRecord,
                                 payload: dict[str, Any]) -> None:
        event = str(payload.get("event") or "")
        if event == "manual_action_required":
            message = str(payload.get("message") or "需要人工操作")
            reason = f"MANUAL_ACTION_REQUIRED: {message}"
            self.store.set_task_status(task.uid, TaskStatus.WAITING_USER.value,
                                       error=reason, force=True)
            self.store.update_run(
                task.run_id, status="waiting_user",
                blocked_reason=f"{task.task_id} · {agent.name}: {message}",
                error=reason,
            )
            self.audit.log("control-plane", "task.manual_action_required",
                           subject=task.uid, payload={
                               "agent_id": agent.agent_id,
                               "kind": payload.get("kind", ""),
                               "message": message,
                               "instructions": payload.get("instructions", ""),
                               "error_code": payload.get("error_code", ""),
                           })
            self.checkpoints.create("task_manual_action", run_id=task.run_id,
                                    task_uid=task.uid,
                                    extra={"kind": payload.get("kind", "")})
            if self.bus:
                self.bus.publish("manual_action", {
                    "task_uid": task.uid, "task_id": task.task_id,
                    "agent_id": agent.agent_id, "agent_name": agent.name,
                    "model": task.spec.model, "phase": "required",
                    "kind": payload.get("kind", ""), "message": message,
                    "instructions": payload.get("instructions", ""),
                    "prompt_file": payload.get("prompt_file", ""),
                    "prompt_path": payload.get("prompt_path", ""),
                }, source=agent.agent_id, correlation_id=task.uid, sync_level=5)
            return
        if event in ("manual_action_resolved", "manual_action_failed"):
            current = self.store.get_task(task.uid)
            waiting = current is not None and \
                current.status == TaskStatus.WAITING_USER.value
            if event == "manual_action_resolved":
                if waiting:
                    self.store.set_task_status(task.uid, TaskStatus.RUNNING.value,
                                               error="", force=True)
                others = [t for t in self.store.list_tasks(task.run_id)
                          if t.uid != task.uid
                          and t.status == TaskStatus.WAITING_USER.value]
                run = self.store.get_run(task.run_id) or {}
                if not others and run.get("status") == "waiting_user":
                    self.store.update_run(task.run_id, status="running",
                                          blocked_reason="", error="")
            self.audit.log("control-plane", f"task.{event}",
                           subject=task.uid, payload={
                               "agent_id": agent.agent_id,
                               "outcome": payload.get("outcome", ""),
                               "detail": payload.get("detail", {}),
                           })
            if self.bus:
                self.bus.publish("manual_action", {
                    "task_uid": task.uid, "task_id": task.task_id,
                    "agent_id": agent.agent_id, "phase": event.split("_")[-1],
                    "outcome": payload.get("outcome", ""),
                }, source=agent.agent_id, correlation_id=task.uid, sync_level=5)

    def _is_active(self, uid: str) -> bool:
        with self._active_tasks_lock:
            return uid in self._active_tasks

    # ------------------------------------------------------------------
    # Start / loop
    # ------------------------------------------------------------------
    def _cancel_event(self, run_id: str) -> threading.Event:
        with self._cancel_lock:
            return self._cancel_events.setdefault(run_id, threading.Event())

    def cancel(self, run_id: str, reason: str = "用户停止运行") -> dict[str, Any]:
        """Hard-stop a run: cancel nodes, abort readers, stop retries."""
        run = self.store.get_run(run_id)
        if not run:
            raise NotFoundError(f"运行不存在: {run_id}")
        event = self._cancel_event(run_id)
        event.set()
        tasks = self.store.list_tasks(run_id)
        canceled: list[str] = []
        for task in tasks:
            if task.status in TERMINAL:
                continue
            self.store.set_task_status(task.uid, TaskStatus.CANCELED.value,
                                       error=reason, force=True)
            self.leases.release(task.uid)
            canceled.append(task.task_id)
        self.store.update_run(run_id, status="canceled", goal_status="blocked",
                              blocked_reason=reason, error=reason,
                              finished_at=now_iso())
        self.audit.log("user", "run.canceled", subject=run_id,
                       payload={"reason": reason, "tasks": canceled})
        self.checkpoints.create("run_canceled", run_id=run_id,
                                extra={"reason": reason, "tasks": canceled})
        if self.bus:
            self.bus.publish("workflow", {
                "phase": "run_canceled", "run_id": run_id,
                "reason": reason, "tasks": canceled,
            }, source="user", correlation_id=run_id, sync_level=2)
        # Best-effort: stop the visible WorkBuddy turn so it does not keep
        # streaming after the control plane has canceled the run.
        try:
            from ..gui.cdp import stop_workbuddy_task
            active_agents = {t.assigned_agent for t in tasks
                             if t.status not in TERMINAL and t.assigned_agent}
            for agent_id in active_agents:
                agent = self.store.get_agent(agent_id)
                app = str(((agent.adapter_config or {}) if agent else {}).get("gui_app") or "")
                if app == "workbuddy":
                    result = stop_workbuddy_task(
                        int((agent.adapter_config or {}).get("workbuddy_cdp_port")
                            or 9223))
                    self.audit.log("control-plane", "workbuddy.stop_requested",
                                   subject=agent_id, payload=result)
        except Exception as exc:
            self.audit.log("control-plane", "workbuddy.stop_failed",
                           subject=run_id, payload={"error": str(exc)[:500]})
        return {"status": "canceled", "run_id": run_id, "reason": reason,
                "tasks": canceled}

    def start(self, run_id: str, *, max_parallel: int | None = None, background: bool = False) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise NotFoundError(f"运行不存在: {run_id}")
        if run["status"] == "canceled":
            return {"status": "canceled", "run_id": run_id,
                    "message": "运行已被用户停止；请创建新的运行"}
        if run["status"] in ("completed", "pending_acceptance", "pending_merge", "released"):
            return {"status": run["status"], "run_id": run_id,
                    "message": "运行已结束或被治理门禁阻塞，请按审批流程处理"}
        event = self._cancel_event(run_id)
        if event.is_set():
            with self._cancel_lock:
                event = self._cancel_events[run_id] = threading.Event()
        updates: dict[str, Any] = {}
        if not int(run.get("max_rounds") or 0):
            updates["max_rounds"] = max(1, int(self.config.max_rounds))
        if not run.get("goal_status"):
            updates["goal_status"] = "active"
        if run.get("status") == "waiting_user":
            updates["blocked_reason"] = ""
            updates["error"] = ""
        if updates:
            self.store.update_run(run_id, **updates)
            run.update(updates)
        lock = self._run_locks.setdefault(run_id, threading.Lock())
        if lock.locked() and not background:
            raise ConflictError("该运行已在执行中")
        if background:
            thread = threading.Thread(target=self._locked_loop, args=(run_id, max_parallel),
                                      name=f"agentloop-run-{run_id}", daemon=True)
            thread.start()
            return {"status": "started", "run_id": run_id, "background": True}
        return self._locked_loop(run_id, max_parallel)

    def _locked_loop(self, run_id: str, max_parallel: int | None) -> dict[str, Any]:
        lock = self._run_locks.setdefault(run_id, threading.Lock())
        acquired = lock.acquire(blocking=False)
        if not acquired:
            return {"status": "busy", "run_id": run_id}
        try:
            return self._run_loop(run_id, max_parallel)
        finally:
            lock.release()

    def _run_loop(self, run_id: str, max_parallel: int | None) -> dict[str, Any]:
        while True:
            result = self._run_round(run_id, max_parallel)
            if result.get("status") != "round_continuing":
                return result
            self.audit.log("control-plane", "round.loop_continue", subject=run_id,
                           payload={"round": result.get("round"),
                                    "tasks": result.get("planned_tasks", []),
                                    "plan_source": result.get("plan_source", "")})

    def _run_round(self, run_id: str, max_parallel: int | None) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        plan = self.store.get_plan(run["plan_id"])
        if not plan:
            raise NotFoundError(f"计划不存在: {run['plan_id']}")
        self.store.update_run(run_id, status="running",
                              started_at=run["started_at"] or now_iso(),
                              blocked_reason="", error="")
        run_workspace = self._ensure_run_workspace(run_id, run["base_commit"])
        round_number = int(run.get("round_number") or 1)
        self.audit.log("control-plane", "run.started", subject=run_id,
                       payload={"max_parallel": max_parallel or self.config.max_parallel,
                                "workspace": run_workspace,
                                "round": round_number,
                                "max_rounds": int(run.get("max_rounds") or 0)})
        self.checkpoints.create(f"round_{round_number}_started", run_id=run_id,
                                extra={"round": round_number})
        self.memory.sync_workflow(run_id)
        self.memory.sync_task_state(run_id)
        self._active_run_id = run_id
        max_workers = max(1, int(max_parallel or self.config.max_parallel))
        executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"task-{run_id[-6:]}")
        futures: dict[str, Future] = {}
        circuit_break = False
        try:
            while True:
                live = self.store.get_run(run_id) or {}
                if live.get("status") == "canceled":
                    self.audit.log("user", "run.canceled_observed", subject=run_id,
                                   payload={"reason": live.get("error", "")})
                    return {"status": "canceled", "run_id": run_id,
                            "reason": live.get("error", "")}
                if live.get("status") == "paused":
                    for task in self.store.list_tasks(run_id):
                        if task.status == TaskStatus.QUEUED.value:
                            self.store.set_task_status(task.uid, TaskStatus.PAUSED.value,
                                                       error="用户暂停", force=True)
                    self.audit.log("user", "run.paused", subject=run_id,
                                   payload={"reason": live.get("error", "")})
                    self.checkpoints.create("run_paused", run_id=run_id)
                    self.memory.sync_workflow(run_id)
                    return {"status": "paused", "run_id": run_id, "reason": "user"}
                tasks = self.store.list_tasks(run_id)
                capacity = max(0, max_workers - len(futures))
                dispatch = self.scheduler.evaluate(tasks, capacity=capacity)
                for task in dispatch.blocked:
                    self.store.set_task_status(task.uid, TaskStatus.BLOCKED.value,
                                               error="前置任务失败/取消", force=True)
                    self.audit.log("control-plane", "task.blocked", subject=task.uid,
                                   payload={"dependencies": task.spec.dependencies})
                if dispatch.should_break:
                    circuit_break = True
                    for task in tasks:
                        if task.status == TaskStatus.QUEUED.value:
                            self.store.set_task_status(task.uid, TaskStatus.PAUSED.value,
                                                       error="连续失败触发熔断", force=True)
                    self.audit.log("control-plane", "run.circuit_breaker", subject=run_id,
                                   payload={"failures": [t.task_id for t in dispatch.final_failures],
                                            "threshold": self.config.max_failures_before_circuit_break})
                    self.checkpoints.create("circuit_breaker", run_id=run_id,
                                            extra={"failures": [t.task_id for t in dispatch.final_failures]})
                    self.memory.sync_workflow(run_id)
                    break

                if not self.scheduler.has_work(tasks):
                    break
                if not futures and not dispatch.ready:
                    break
                for task in dispatch.ready:
                    if task.uid not in futures:
                        futures[task.uid] = executor.submit(self._safe_execute, task.uid)
                if futures:
                    done, _ = wait(list(futures.values()), return_when=FIRST_COMPLETED, timeout=30)
                    for fut in done:
                        uid = next((k for k, v in futures.items() if v is fut), None)
                        if uid:
                            futures.pop(uid, None)
                        try:
                            fut.result()
                        except Exception as exc:
                            self._mark_crashed(uid or "", exc)
                    continue
                break
        finally:
            executor.shutdown(wait=True, cancel_futures=False)
            self._active_run_id = ""

        if circuit_break:
            self.store.update_run(run_id, status="paused",
                                  error="连续失败达到阈值，已停止自动重试")
            return {"status": "paused", "run_id": run_id,
                    "reason": "circuit_breaker"}
        return self._finalize_run(run_id, plan)

    def _safe_execute(self, uid: str) -> None:
        try:
            self._execute_task(uid)
        except Exception as exc:
            self._mark_crashed(uid, exc)

    def _mark_crashed(self, uid: str, exc: Exception) -> None:
        task = self.store.get_task(uid) if uid else None
        if not task:
            return
        import traceback
        stack = traceback.format_exc()
        try:
            self.store.set_task_status(uid, TaskStatus.FAILED.value, error=f"控制平面异常: {exc}", force=True)
        except Exception:
            pass
        self.leases.release(uid)
        try:
            log = ensure_dir(Path(task.task_dir) / "logs") / "control-plane-error.log"
            atomic_write_text(log, stack)
        except Exception:
            pass
        self.audit.log("control-plane", "task.crashed", subject=uid,
                       payload={"error": str(exc)[:1000], "traceback": stack[-2000:]})

    def _block_dependent_on_failures(self, tasks: list[Task]) -> None:
        by_id = {t.task_id: t for t in tasks}
        for task in tasks:
            if task.status != TaskStatus.QUEUED.value:
                continue
            blocked = False
            for dep in task.spec.dependencies:
                dep_task = by_id.get(dep)
                if dep_task and dep_task.status in (TaskStatus.FAILED.value, TaskStatus.CANCELED.value,
                                                    TaskStatus.REJECTED.value,
                                                    TaskStatus.BLOCKED.value):
                    blocked = True
                    break
            if blocked:
                self.store.set_task_status(task.uid, TaskStatus.BLOCKED.value,
                                           error="前置任务失败/取消", force=True)
                task.status = TaskStatus.BLOCKED.value
                task.error = "前置任务失败/取消"
                self.audit.log("control-plane", "task.blocked", subject=task.uid,
                               payload={"dependencies": task.spec.dependencies})

    # ------------------------------------------------------------------
    # Single task
    # ------------------------------------------------------------------
    def _execute_task(self, uid: str) -> None:
        task = self.store.get_task(uid)
        if not task or task.status not in (TaskStatus.QUEUED.value, TaskStatus.WAITING_USER.value):
            return
        run = self.store.get_run(task.run_id)
        plan = self.store.get_plan(task.plan_id)
        if not run or not plan:
            self.store.set_task_status(uid, TaskStatus.FAILED.value, error="运行或计划缺失", force=True)
            return
        decision = None
        pinned = self._pinned_agent(task)
        if pinned is not None:
            agent = pinned
        else:
            if self.router:
                decision = self.router.select(task)
            agent = decision.agent if decision else self._select_agent(task)
        if not agent:
            self.store.set_task_status(uid, TaskStatus.FAILED.value, error="没有可用的执行 Agent", force=True)
            self.audit.log("control-plane", "task.no_agent", subject=uid,
                           payload={"required": list(task.spec.required_capabilities)})
            return

        allowed, reason = self.budget.can_start(task)
        if not allowed:
            approval = self.approvals.ensure_budget_approval(task, self.budget.used_project(),
                                                             self.budget.project_budget)
            if approval.status != "approved":
                self.store.set_task_status(uid, TaskStatus.WAITING_USER.value, error=reason, force=True)
                self.audit.log("control-plane", "task.budget_blocked", subject=uid,
                               payload={"reason": reason, "approval": approval.approval_id})
                return
            self.audit.log("control-plane", "task.budget_override", subject=uid,
                           payload={"approval": approval.approval_id,
                                    "used": self.budget.used_project(),
                                    "budget": self.budget.project_budget})

        reasons = self.approvals.high_risk_reasons(task)
        if reasons:
            approval = self.approvals.ensure_high_risk_approval(task, agent.name, reasons)
            if approval and approval.status != "approved":
                self.store.set_task_status(uid, TaskStatus.WAITING_USER.value,
                                           error="等待高风险操作审批", force=True)
                self.audit.log("control-plane", "task.waiting_approval", subject=uid,
                               payload={"approval": approval.approval_id, "reasons": reasons})
                return

        if task.spec.writable:
            conflicts = self.leases.conflicts(self.project.project_id, task)
            if conflicts:
                self.store.set_task_status(uid, TaskStatus.BLOCKED.value,
                                           error="文件范围与其他任务冲突", force=True)
                self.audit.log("control-plane", "task.lease_conflict", subject=uid,
                               payload={"conflicts": conflicts})
                return

        task = self.store.get_task(uid) or task
        task.assigned_agent = task.assigned_agent or agent.agent_id
        task.attempts += 1
        workdir = self._prepare_workdir(task, run)
        upstream = self.context.upstream_results(task)
        if self.cost and upstream:
            saved = 0
            for item in upstream:
                full = self.store.get_result(f"{task.run_id}:{item['task_id']}")
                if full:
                    saved += max(
                        0,
                        estimate_tokens(json.dumps(full["payload"], ensure_ascii=False, default=str))
                        - estimate_tokens(json.dumps(item, ensure_ascii=False, default=str)),
                    )
            self.cost.record_saving(
                "intermediate_reuse", saved, task_uid=uid, run_id=task.run_id,
                detail={"upstream": [item["task_id"] for item in upstream]},
            )
        hints = self.cost.intermediate_hints(task) if self.cost else []
        bootstrap = self.memory.bootstrap(scope=task.task_id, agent_id=agent.agent_id)
        extra_context = {
            "run_base_commit": run["base_commit"],
            "verifier_available": bool(self.config.verifier_agent_id),
            "upstream_results": upstream,
            "intermediate_hints": hints,
            "router": decision.to_dict() if decision else {},
            "round_context": self._task_round_context(run, task),
        }
        request_model = self._model_for_agent(agent, task)
        try:
            agent_profile = self._agent_profile(agent, task, request_model)
        except ValidationError as exc:
            reason = f"模型配置与 {agent.agent_id} 的真实选项不符: {exc.message}"
            self.store.set_task_status(uid, TaskStatus.FAILED.value,
                                       error=reason, force=True)
            self.audit.log("control-plane", "task.profile_invalid", subject=uid,
                           payload={"agent_id": agent.agent_id,
                                    "model": request_model,
                                    "error": exc.message,
                                    "details": exc.details})
            return
        extra_context["agent_profile"] = agent_profile
        if self.cost:
            context_budget = self.config.context_findings_tokens
            if agent_profile["context_length"]:
                # Respect a user-declared small context window by shrinking the
                # intermediate context; never inflate beyond global defaults.
                context_budget = min(
                    context_budget,
                    max(1024, int(agent_profile["context_length"]) // 4),
                )
            extra_context = self.cost.compress_context(
                extra_context, context_budget,
                kind="context_compression", task_uid=uid, run_id=task.run_id,
            )
        task_pkg = self.prompts.write_task_package(
            task, agent, plan, self.memory.injectable(scope=task.task_id),
            runtime_dir=workdir, extra_context=extra_context, bootstrap=bootstrap,
            agent_profile=agent_profile,
        )
        self.leases.acquire(self.project.project_id, task)
        task.workdir = workdir
        task.task_dir = str(task_pkg.task_dir)
        task.assigned_agent = agent.agent_id
        self.store.update_task(
            uid, assigned_agent=agent.agent_id, attempts=task.attempts,
            workdir=workdir, task_dir=str(task_pkg.task_dir), patch_path=task.patch_path,
        )
        self.store.set_task_status(uid, TaskStatus.RUNNING.value)
        self.checkpoints.create("task_dispatched", run_id=task.run_id, task_uid=uid,
                                extra={"agent": agent.agent_id, "attempt": task.attempts})
        self.audit.log("control-plane", "task.dispatched", subject=uid,
                       payload={"agent": agent.agent_id, "attempt": task.attempts,
                                "workdir": workdir, "priority": task.spec.priority})
        if self.bus:
            prompt_text = read_text(task_pkg.prompt_path, default="")
            self.bus.publish("prompt", {
                "purpose": "task", "task_uid": uid, "task_id": task.task_id,
                "agent_id": agent.agent_id, "model": self._model_for_agent(agent, task),
                "role": task.spec.role, "pinned_model": bool(task.spec.model),
                "prompt_path": str(task_pkg.prompt_path),
                "workdir": workdir, "priority": task.spec.priority,
                "prompt_tokens": estimate_tokens(prompt_text),
                "preview": prompt_text[:1200],
                "required_capabilities": list(task.spec.required_capabilities),
            }, source=agent.agent_id, correlation_id=uid, sync_level=2)
        cache_key, file_hash = self._analysis_files(task, workdir)
        cached = self.store.cache_get(cache_key) if cache_key else None
        cache_source = "exact_cache" if cached is not None else ""
        cache_meta: dict[str, Any] = {}
        if cached is None and self.cost:
            semantic = self.cost.semantic_lookup(task, file_hash)
            if semantic:
                cached = semantic["payload"]
                cache_source = "semantic_cache"
                cache_meta = {"cache_key": semantic["cache_key"],
                              "similarity": semantic["similarity"],
                              "matched_objective": semantic["source_task"]}
        if cached is not None:
            payload = dict(cached)
            payload["task_id"] = task.task_id
            payload["agent_id"] = agent.agent_id
            try:
                result = ResultPackage.validate(
                    payload, expected_task_id=task.task_id, expected_agent_id=agent.agent_id
                )
            except ValidationError:
                result = None
            if result is not None:
                if cache_source == "exact_cache" and self.cost:
                    tokens_saved = (
                        estimate_tokens(read_text(task_pkg.prompt_path, default=""))
                        + estimate_tokens(json.dumps(cached, ensure_ascii=False, default=str))
                    )
                    self.cost.record_saving(
                        "exact_cache", tokens_saved, task_uid=uid, run_id=task.run_id,
                        detail={"cache_key": cache_key},
                    )
                self.store.save_result(task, result, valid=True)
                self.store.set_task_status(uid, TaskStatus.VERIFYING.value)
                self.audit.log("control-plane", "task.cache_hit", subject=uid,
                               payload={"cache_key": cache_key, "source": cache_source,
                                        "meta": cache_meta})
                self.checkpoints.create("task_cache_hit", run_id=task.run_id, task_uid=uid)
                self._finish_verification(task, agent, result)
                return
        request = ExecutionRequest(
            project_id=self.project.project_id, project_root=self.project.root,
            task=task, workdir=workdir, task_dir=str(task_pkg.task_dir),
            prompt_path=str(task_pkg.prompt_path), result_path=str(task_pkg.result_path),
            model=request_model,
            context_length=int(agent_profile["context_length"]),
            capability_level=str(agent_profile["capability_level"]),
            timeout_seconds=task.spec.time_limit_minutes * 60,
            purpose="task", full_control=bool(self.config.full_control),
            notify=lambda payload, _task=task, _agent=agent:
                self._on_adapter_notification(_task, _agent, payload),
            cancel_event=self._cancel_event(task.run_id),
        )
        adapter = create_adapter(agent)
        # A desktop GUI Agent has exactly one composer/session.  Two parallel
        # tasks pinned to the same GUI app would overwrite each other's prompt
        # and workspace, so serialize GUI execution per agent while still
        # allowing different GUI/CLI agents to run in parallel.
        gui_lock = self._agent_lock(agent.agent_id) \
            if agent.adapter_type == "gui_automation" else None
        with self._active_tasks_lock:
            self._active_tasks.add(uid)
        try:
            if gui_lock is not None:
                with gui_lock:
                    outcome = adapter.execute(request)
            else:
                outcome = adapter.execute(request)
        finally:
            with self._active_tasks_lock:
                self._active_tasks.discard(uid)
        run_now = self.store.get_run(task.run_id) or {}
        cancel_event = self._cancel_events.get(task.run_id)
        if run_now.get("status") == "canceled" or (
                cancel_event is not None and cancel_event.is_set()):
            self.store.set_task_status(uid, TaskStatus.CANCELED.value,
                                       error="用户停止运行", force=True)
            self.leases.release(uid)
            return
        interface = (outcome.extras or {}).get("interface") or {}
        verify = interface.get("verify") if isinstance(interface, dict) else {}
        verify = verify if isinstance(verify, dict) else {}
        if verify:
            self.audit.log("control-plane", "workspace.verified", subject=uid,
                           payload={"expected": verify.get("expected", {}),
                                    "actual": verify.get("actual", {}),
                                    "evidence": verify.get("evidence", {}),
                                    "method": interface.get("method", "")})
            if self.config.workspace_verify and not verify.get("ok", True):
                self._retry_or_fail(
                    task,
                    f"{verify.get('error_code') or 'WORKSPACE_MISMATCH'}: "
                    f"{verify.get('reason', '')}",
                )
                return
        elif interface.get("method") == "gui" and self.config.workspace_verify:
            self.audit.log("control-plane", "workspace.unverified", subject=uid,
                           payload={"agent": agent.agent_id,
                                    "reason": interface.get("fallback_reason", "")})
        self.store.update_task(uid, pid=outcome.pid)
        self.budget.record(
            task, agent.agent_id, request.model, outcome.usage,
            duration_ms=outcome.duration_ms, run_id=task.run_id, status=outcome.status,
        )
        self.audit.log(agent.agent_id, "task.executed", subject=uid,
                       payload={"status": outcome.status, "duration_ms": outcome.duration_ms,
                                "error": outcome.error[:500], "session": outcome.session_id})
        if outcome.result_payload is not None:
            write_json(task_pkg.persistent_result_path, outcome.result_payload)
        if self.bus and (outcome.result_payload is None or outcome.status != "completed"):
            self.bus.publish("response", {
                "purpose": "task", "task_uid": uid, "task_id": task.task_id,
                "agent_id": agent.agent_id, "status": outcome.status,
                "error": outcome.error[:500], "usage": outcome.usage.to_dict(),
                "has_result": outcome.result_payload is not None,
                "duration_ms": outcome.duration_ms, "session_id": outcome.session_id,
            }, source=agent.agent_id, correlation_id=uid)
        if outcome.status in ("waiting_user", "waiting_agent"):
            message = outcome.error or outcome.text or "等待 Agent 执行"
            self.store.set_task_status(uid, TaskStatus.WAITING_USER.value,
                                       error=message, force=True)
            self.store.update_run(
                task.run_id, status="waiting_user",
                blocked_reason=f"{task.task_id} · {agent.name}: {message}",
                error=message,
            )
            self.audit.log("control-plane", "task.waiting_user", subject=uid,
                           payload={"error": message, "extras": outcome.extras})
            self.checkpoints.create("task_waiting_user", run_id=task.run_id, task_uid=uid)
            self.memory.sync_task_state(task.run_id)
            self.memory.sync_workflow(task.run_id)
            return
        if outcome.status != "completed":
            self._retry_or_fail(task, f"{outcome.status}: {outcome.error}")
            return
        try:
            (Path(request.task_dir) / "manual-action.json").unlink()
        except FileNotFoundError:
            pass
        payload = outcome.result_payload if outcome.result_payload is not None else read_json(task_pkg.persistent_result_path)
        if payload is None:
            self._retry_or_fail(task, "成果包缺失：Agent 未写入 result.json")
            return
        self._finalize_from_result(task, agent, payload)

    def _analysis_files(self, task: Task, workdir: str) -> tuple[str, str]:
        """Return (cache_key, file_hash) for a read-only task with a scope."""
        if task.spec.type not in ("analysis", "review") or not task.spec.scope:
            return "", ""
        base = Path(workdir)
        if not base.exists():
            return "", ""
        files: list[str] = []
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(base).as_posix()
            if rel.startswith(".agentloop-task/") or rel.startswith(".git/"):
                continue
            if any(scope_matches(pattern, rel) for pattern in task.spec.scope):
                files.append(rel)
        file_hash = tree_hash(files, base=base)
        key = sha1_hex(stable_json({
            "project_id": task.project_id,
            "task_id": task.task_id,
            "objective": task.spec.objective,
            "type": task.spec.type,
            "scope": task.spec.scope,
            "out_of_scope": task.spec.out_of_scope,
            "acceptance": task.spec.acceptance_criteria,
            "dependencies": task.spec.dependencies,
            "agent": task.assigned_agent,
            "file_hash": file_hash,
        }))
        return key, file_hash

    def _cache_lookup(self, task: Task, workdir: str) -> dict[str, Any] | None:
        key, _ = self._analysis_files(task, workdir)
        if not key:
            return None
        cached = self.store.cache_get(key)
        if cached is None:
            return None
        return cached if isinstance(cached, dict) else None

    def _cache_store(self, task: Task, result: ResultPackage) -> None:
        key, file_hash = self._analysis_files(task, task.workdir)
        if not key:
            return
        self.store.cache_set(key, task.project_id, task.spec.type, file_hash, result.payload)
        if self.cost:
            self.cost.semantic_store(task, file_hash, result.payload)

    def _ensure_run_workspace(self, run_id: str, base_commit: str) -> str:
        target = self.paths.run_workspace(run_id)
        with self._wt_lock:
            if Path(target).exists():
                proc = run_git(["rev-parse", "--is-inside-work-tree"], cwd=target, check=False)
                if proc.returncode == 0:
                    return str(target)
                shutil.rmtree(target, ignore_errors=True)
            try:
                create_worktree(self.project.root, target, base_commit or self.project.commit)
            except Exception as exc:
                self.audit.log("control-plane", "workspace.create_failed", subject=run_id,
                               payload={"expected": str(target), "error": str(exc)[:300]})
                return str(self.project.root)
        self.audit.log("control-plane", "workspace.created", subject=run_id,
                       payload={"expected": str(target), "kind": "run",
                                "base_commit": base_commit or self.project.commit})
        return str(target)

    def _prepare_workdir(self, task: Task, run: dict[str, Any]) -> str:
        target = self.paths.worktree_dir(task.run_id, task.task_id)
        with self._wt_lock:
            reused = False
            if Path(target).exists():
                # A worktree left by an interrupted run. Reuse only if git agrees.
                proc = run_git(["rev-parse", "--is-inside-work-tree"], cwd=target, check=False)
                if proc.returncode == 0:
                    reused = True
                else:
                    shutil.rmtree(target, ignore_errors=True)
            if not reused:
                create_worktree(self.project.root, target, run["base_commit"])
            ensure_dir(target)
        self._apply_upstream_patches(task, str(target))
        return str(target)

    def _upstream_task_order(self, task: Task) -> list[Task]:
        tasks = {t.task_id: t for t in self.store.list_tasks(task.run_id)}
        order: list[Task] = []
        seen: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in seen:
                return
            upstream = tasks.get(task_id)
            if not upstream:
                return
            for dep in upstream.spec.dependencies:
                visit(dep)
            seen.add(task_id)
            if upstream.patch_path:
                order.append(upstream)

        for dep in task.spec.dependencies:
            visit(dep)
        return order

    def _upstream_patch_paths(self, task: Task) -> list[str]:
        return [t.patch_path for t in self._upstream_task_order(task)
                if t.patch_path and Path(t.patch_path).exists()
                and Path(t.patch_path).stat().st_size > 0]

    def _apply_upstream_patches(self, task: Task, workdir: str) -> None:
        """Compose a dependency chain into the task worktree.

        Every task starts from the run's base commit.  A downstream task must
        see the files produced by its upstream tasks, otherwise T003/T004/T005
        style build chains each operate on an empty checkout.  Apply dependency
        patches and commit them as the worktree baseline so the task's own
        patch only contains its own changes.
        """
        order = self._upstream_task_order(task)
        applied: list[str] = []
        for upstream in order:
            patch = Path(upstream.patch_path)
            if not patch.exists() or patch.stat().st_size == 0:
                continue
            reverse = run_git(["apply", "--reverse", "--check", str(patch)],
                              cwd=workdir, check=False)
            if reverse.returncode == 0:
                continue
            # A retried task reuses its worktree, so the upstream patch is
            # already committed in HEAD while the working tree also contains
            # this task's unfinished edits.  Check the index/HEAD instead of
            # reapplying and conflicting with those edits.
            indexed = run_git(["apply", "--cached", "--reverse", "--check", str(patch)],
                              cwd=workdir, check=False)
            if indexed.returncode == 0:
                continue
            ok, detail = apply_patch(workdir, patch, three_way=False)
            if not ok:
                raise ConflictError(
                    f"{task.task_id}: 上游补丁无法应用 {upstream.task_id}",
                    details={"patch": str(patch), "detail": detail[:800]},
                )
            applied.append(upstream.task_id)
        if applied:
            identity = ["-c", "user.name=AgentMesh", "-c", "user.email=agentmesh@localhost"]
            run_git([*identity, "add", "-A"], cwd=workdir, check=False)
            run_git([*identity, "commit", "--allow-empty", "-m",
                     f"chore: integrate {', '.join(applied)} into {task.task_id}"],
                    cwd=workdir, check=False)
            self.audit.log("control-plane", "task.upstream_patches_applied",
                           subject=task.uid,
                           payload={"upstream": applied, "workdir": workdir})

    def _select_agent(self, task: Task) -> AgentRecord | None:
        agents = self.store.list_agents()
        if task.assigned_agent:
            match = next((a for a in agents if a.agent_id == task.assigned_agent), None)
            if match:
                return match
        suggested = task.spec.suggested_agent
        if suggested:
            match = next((a for a in agents if a.agent_id == suggested or a.name.lower() == suggested.lower()), None)
            if match:
                return match
        perms = task.spec.required_capabilities
        candidates = []
        for agent in agents:
            if agent.status == "offline":
                continue
            if task.spec.writable and not agent.capabilities.get("write_files"):
                continue
            if any(not agent.capabilities.get(cap) for cap in perms):
                continue
            candidates.append(agent)
        candidates.sort(key=lambda a: (a.access_level, a.agent_id), reverse=True)
        main = self.store.get_main_node(self.project.project_id)
        non_main = [a for a in candidates if main and a.agent_id != main["agent_id"]]
        return (non_main or candidates or [None])[0]

    @staticmethod
    def _estimate(value: Any) -> int:
        return estimate_tokens(json.dumps(value, ensure_ascii=False, default=str))

    def _pinned_agent(self, task: Task) -> AgentRecord | None:
        """Use the role-planner's decision unless that agent became unusable.

        Pinning is what makes cost control real: the planner already chose a
        concrete (agent, model, tier) for this role, so the generic router must
        not silently swap in a more expensive agent.
        """
        if not task.spec.model:
            return None
        agent_id = task.assigned_agent or task.spec.suggested_agent
        if not agent_id:
            return None
        agent = next((a for a in self.store.list_agents() if a.agent_id == agent_id), None)
        if agent is None or agent.status in ("offline", "error"):
            return None
        return agent

    def _agent_profile(self, agent: AgentRecord, task: Task | None,
                       model: str) -> dict[str, Any]:
        """Per-task override > selected model profile > agent default."""
        extra = agent.extra or {}
        profiles = extra.get("model_profiles") if isinstance(
            extra.get("model_profiles"), dict) else {}
        profile = profiles.get(model) if isinstance(profiles.get(model), dict) else {}
        context_length = int(
            (task.spec.context_length if task else 0)
            or profile.get("context_length")
            or extra.get("context_length") or 0
        )
        capability_level = str(
            (task.spec.capability_level if task else "")
            or profile.get("capability_level")
            or extra.get("capability_level") or ""
        )
        return resolve_profile(
            agent, model, self.config.to_dict(),
            context_length=max(0, context_length),
            capability_level=capability_level,
        )

    def _model_for_agent(self, agent: AgentRecord, task: Task | None = None) -> str:
        pinned_ids = {task.assigned_agent, task.spec.suggested_agent} - {""} if task else set()
        model = str(task.spec.model or "") if task else ""
        if model and (not pinned_ids or agent.agent_id in pinned_ids):
            return model
        selected = str((agent.extra or {}).get("selected_model") or "")
        if selected:
            return selected
        current = self.store.get_main_node(self.project.project_id)
        if current and current["agent_id"] == agent.agent_id and current.get("model"):
            return current["model"]
        if agent.models:
            return agent.models[0]
        return ""

    # ------------------------------------------------------------------
    # Role-based escalation
    # ------------------------------------------------------------------
    def _escalate_task(self, task: Task) -> dict[str, Any] | None:
        """Move a failed retry to the next rung of its role ladder.

        This is the DSH rule "only upgrade when the cheap model cannot deliver"
        implemented as code: escalation happens on retry, only forward, and is
        recorded with the reason so the cost report can show why a premium
        model ended up being used.
        """
        # A model pinned by the WebUI collaboration contract is a user
        # decision, not a cost suggestion. Retry must not silently replace it.
        # The role-ladder solve path has no contract marker and keeps escalating.
        if task.spec.model and str(task.spec.role or "") in {
                "explore", "research", "implement", "review", "write", "verify"}:
            plan = self.store.get_plan(task.plan_id) if task.plan_id else None
            if plan and any("协作契约" in c for c in (plan.constraints or [])):
                return None
        policy = self.roles
        role_name = str(task.spec.role or "")
        if not policy or role_name not in policy.roles:
            return None
        agents = self.store.list_agents()
        catalog = {a.agent_id: list(a.models) for a in agents}
        ladder = policy.candidates(
            role_name, agents=agents, catalog=catalog,
            require_auto=self.config.router_require_auto_executable,
        )
        current_model = str(task.spec.model or "")
        current_key = f"{task.assigned_agent}::{current_model}"
        index = next((i for i, c in enumerate(ladder) if c.key == current_key), -1)
        if index < 0:
            return None
        current_tier = ladder[index].tier if current_model else classify_model(current_model)
        for candidate in ladder[index + 1:]:
            if candidate.tier < (current_tier if current_model else 0):
                continue
            self.store.update_task(
                task.uid, assigned_agent=candidate.agent_id, model=candidate.model,
            )
            payload = {
                "role": role_name, "from": current_key or "unknown",
                "to": candidate.key, "tier": candidate.tier,
                "tier_label": candidate.tier_label, "reason": "执行失败后按角色阶梯升级",
            }
            self.audit.log("control-plane", "task.escalated", subject=task.uid, payload=payload)
            if self.bus:
                self.bus.publish("task_state", {
                    "phase": "escalated", "task_uid": task.uid,
                    "task_id": task.task_id, **payload,
                }, correlation_id=task.uid, sync_level=2)
            return payload
        return None

    # ------------------------------------------------------------------
    # Result -> patch -> verification
    # ------------------------------------------------------------------
    def _retry_or_fail(self, task: Task, error: str) -> None:
        run = self.store.get_run(task.run_id) or {}
        event = self._cancel_events.get(task.run_id)
        if run.get("status") == "canceled" or (event is not None and event.is_set()):
            self.store.set_task_status(task.uid, TaskStatus.CANCELED.value,
                                       error="用户停止运行", force=True)
            self.leases.release(task.uid)
            return
        fresh = self.store.get_task(task.uid) or task
        if fresh.attempts <= task.spec.retry_limit:
            self.store.set_task_status(task.uid, TaskStatus.FAILED.value, error=error, force=True)
            self.store.set_task_status(task.uid, TaskStatus.QUEUED.value, force=True)
            escalation = self._escalate_task(fresh)
            self.audit.log("control-plane", "task.retry", subject=task.uid,
                           payload={"attempt": fresh.attempts, "error": error[:500],
                                    "escalated_to": (escalation or {}).get("to", "")})
            self.checkpoints.create("task_retry", run_id=task.run_id, task_uid=task.uid)
            self.memory.sync_task_state(task.run_id)
            return
        self.store.set_task_status(task.uid, TaskStatus.FAILED.value, error=error, force=True)
        self.leases.release(task.uid)
        self.store.append_audit(self.project.project_id, "control-plane", "task.failed",
                                subject=task.uid, payload={"error": error[:1000]})
        self.checkpoints.create("task_failed", run_id=task.run_id, task_uid=task.uid)
        self.memory.sync_task_state(task.run_id)
        self.memory.sync_workflow(task.run_id)

    def _finalize_from_result(self, task: Task, agent: AgentRecord, payload: dict[str, Any]) -> None:
        normalized, normalized_fields = ResultPackage.normalize(payload)
        if normalized_fields:
            self.audit.log("control-plane", "result.normalized", subject=task.uid,
                           payload={"fields": normalized_fields})
        try:
            result = ResultPackage.validate(
                normalized, expected_task_id=task.task_id, expected_agent_id=agent.agent_id
            )
        except ValidationError as exc:
            self.store.save_result(task, ResultPackage(payload), valid=False, error=exc.message)
            self._retry_or_fail(task, f"成果包校验失败: {exc.message}")
            return
        if result.status == "failed":
            self.store.save_result(task, result, valid=True)
            self._retry_or_fail(task, f"Agent 报告失败: {result.summary}")
            return
        self.store.save_result(task, result, valid=True)
        self.store.set_task_status(task.uid, TaskStatus.VERIFYING.value)
        self._ensure_change(task, agent, result)
        self._finish_verification(task, agent, result)

    def _ensure_change(self, task: Task, agent: AgentRecord, result: ResultPackage) -> None:
        if not task.spec.writable:
            return
        existing = self.store.list_changes(task_uid=task.uid)
        if existing:
            task.patch_path = existing[0].patch_path
            self.store.update_task(task.uid, patch_path=task.patch_path)
            return
        patch_path = str(self.paths.patch_path(task.uid))
        generate_patch(task.workdir, patch_path)
        files = patch_files(patch_path)
        after_hash = tree_hash(files, base=Path(task.workdir)) if files else ""
        change = ChangeRecord(
            change_id=new_id("chg"), project_id=self.project.project_id, task_uid=task.uid,
            agent_id=agent.agent_id, base_commit=task.base_commit,
            base_hash=sha1_hex(task.base_commit), before_hash=sha1_hex(task.base_commit),
            after_hash=after_hash, files=files, patch_path=patch_path,
            status=ChangeStatus.PROPOSED.value, created_at=now_iso(),
        )
        self.store.add_change(change)
        task.patch_path = patch_path
        self.store.update_task(task.uid, patch_path=patch_path)
        self.checkpoints.create("change_recorded", run_id=task.run_id, task_uid=task.uid,
                                extra={"change_id": change.change_id, "files": files})
        self.audit.log(agent.agent_id, "change.recorded", subject=task.uid,
                       payload={"change_id": change.change_id, "files": files,
                                "before": change.before_hash, "after": change.after_hash})

    def _finish_verification(self, task: Task, agent: AgentRecord,
                             result: ResultPackage) -> None:
        """Finish verification without ever leaving a task stuck in verifying."""
        try:
            self._finish_verification_impl(task, agent, result)
        except Exception as exc:
            reason = f"验证执行异常: {exc}"
            self.store.set_task_status(task.uid, TaskStatus.FAILED.value,
                                       error=reason, force=True)
            self.leases.release(task.uid)
            self.audit.log("control-plane", "task.verification_error",
                           subject=task.uid, payload={"error": str(exc)[:500]})
            self.checkpoints.create("task_verification_error", run_id=task.run_id,
                                    task_uid=task.uid)
            self.memory.sync_task_state(task.run_id)
            self.memory.sync_workflow(task.run_id)

    def _finish_verification_impl(self, task: Task, agent: AgentRecord,
                                  result: ResultPackage) -> None:
        outcome: VerificationOutcome = self.verifier.verify(
            task, result, upstream_patches=self._upstream_patch_paths(task))
        self.store.add_verification(
            outcome.report, project_id=self.project.project_id, task_uid=task.uid,
            report_path=outcome.report_path,
        )
        self.audit.log("control-plane", "verification.deterministic", subject=task.uid,
                       payload={"status": outcome.report.status, "checks": outcome.report.checks})
        final_report = outcome.report
        agent_report = self._independent_verification(task, agent)
        if agent_report == "waiting":
            self.store.update_task(task.uid, verification_status=final_report.status)
            self.store.set_task_status(task.uid, TaskStatus.WAITING_USER.value,
                                       error="等待独立验证节点提交验证报告", force=True)
            self.checkpoints.create("verification_waiting", run_id=task.run_id, task_uid=task.uid)
            self.memory.sync_task_state(task.run_id)
            self.memory.sync_workflow(task.run_id)
            return
        if isinstance(agent_report, VerificationReport):
            final_report = self.verifier.combine(task, outcome.report, agent_report)
            self.store.add_verification(
                final_report, project_id=self.project.project_id, task_uid=task.uid,
                report_path=outcome.report_path,
            )
        self.store.update_task(task.uid, verification_status=final_report.status)
        if final_report.status == VerificationStatus.FAILED.value:
            self.store.set_task_status(task.uid, TaskStatus.FAILED.value,
                                       error="独立验证失败: " + "; ".join(final_report.warnings[:3]),
                                       force=True)
            self.leases.release(task.uid)
            self.audit.log("control-plane", "task.verification_failed", subject=task.uid,
                           payload={"warnings": final_report.warnings})
            self.checkpoints.create("task_failed", run_id=task.run_id, task_uid=task.uid)
            self.memory.record_problem(
                symptom=f"任务 {task.task_id} 验证失败", cause="; ".join(final_report.warnings[:5]),
                fix="查看验证报告并修复后重新执行", verification="control-plane verification",
                task_uid=task.uid,
            )
            self.memory.sync_task_state(task.run_id)
            self.memory.sync_workflow(task.run_id)
            return
        if not task.spec.writable:
            try:
                self._cache_store(task, result)
            except Exception as exc:
                self.audit.log("control-plane", "task.cache_store_failed", subject=task.uid,
                               payload={"error": str(exc)[:300]})
        self.store.set_task_status(task.uid, TaskStatus.COMPLETED.value)
        self.leases.release(task.uid)
        self.checkpoints.create("task_completed", run_id=task.run_id, task_uid=task.uid,
                                extra={"verification": final_report.status})
        self.audit.log("control-plane", "task.completed", subject=task.uid,
                       payload={"verification": final_report.status})
        self.memory.sync_task_state(task.run_id)
        self.memory.sync_workflow(task.run_id)

    def _independent_verification(self, task: Task, executor: AgentRecord) -> VerificationReport | str | None:
        verifier = self._select_verifier(executor)
        if not verifier:
            return None
        directory = ensure_dir(self.paths.run_dir(task.run_id) / "verification" / task.task_id)
        report_path = directory / "verification.json"
        if report_path.exists():
            data = read_json(report_path)
            if data:
                try:
                    return VerificationReport.validate(
                        data, expected_task_id=task.task_id, executor_agent=executor.agent_id
                    )
                except ValidationError as exc:
                    self.audit.log("control-plane", "verification.invalid_report", subject=task.uid,
                                   payload={"error": exc.message})
                    return None
        result = self.store.get_result(task.uid) or {}
        summary = (result.get("payload") or {})
        prompt_path, expected_report = self.prompts.write_verification_request(
            run_id=task.run_id, task=task, verifier=verifier,
            model=self._model_for_agent(verifier), patch_path=task.patch_path,
            result_summary={
                "task_id": task.task_id, "summary": summary.get("summary"),
                "files_changed": summary.get("files_changed"), "tests": summary.get("tests"),
                "evidence": summary.get("evidence"), "risks": summary.get("risks"),
            },
            verification_commands=task.spec.verification_commands,
        )
        if self.bus:
            self.bus.publish("prompt", {
                "purpose": "verify", "task_uid": task.uid, "task_id": task.task_id,
                "agent_id": verifier.agent_id, "prompt_path": str(prompt_path),
                "patch_path": task.patch_path,
                "preview": read_text(prompt_path, default="")[:1200],
            }, source=verifier.agent_id, correlation_id=task.uid)
        synthetic = Task(
            uid=f"{task.uid}:verify", project_id=self.project.project_id,
            plan_id=task.plan_id, run_id=task.run_id, spec=task.spec,
            status=TaskStatus.RUNNING.value, workdir=self.project.root,
            task_dir=str(prompt_path.parent), base_commit=task.base_commit,
        )
        request = ExecutionRequest(
            project_id=self.project.project_id, project_root=self.project.root,
            task=synthetic, workdir=self.project.root, task_dir=str(prompt_path.parent),
            prompt_path=str(prompt_path), result_path=str(expected_report),
            model=self._model_for_agent(verifier),
            timeout_seconds=self.config.verify_timeout_seconds, purpose="verify",
            full_control=bool(self.config.full_control),
        )
        outcome = create_adapter(verifier).execute(request)
        self.budget.record(
            synthetic, verifier.agent_id, request.model, outcome.usage,
            duration_ms=outcome.duration_ms, run_id=task.run_id, status=outcome.status,
        )
        if self.bus:
            self.bus.publish("response", {
                "purpose": "verify", "task_uid": task.uid, "task_id": task.task_id,
                "agent_id": verifier.agent_id, "status": outcome.status,
                "error": outcome.error[:500], "usage": outcome.usage.to_dict(),
                "report_path": str(expected_report),
            }, source=verifier.agent_id, correlation_id=task.uid)
        if outcome.status in ("waiting_user", "waiting_agent"):
            self.audit.log(verifier.agent_id, "verification.waiting", subject=task.uid,
                           payload={"agent": verifier.agent_id})
            return "waiting"
        data = outcome.result_payload if outcome.result_payload is not None else read_json(expected_report)
        if data is None:
            self.audit.log(verifier.agent_id, "verification.no_report", subject=task.uid,
                           payload={"error": outcome.error[:300]})
            return None
        try:
            return VerificationReport.validate(
                data, expected_task_id=task.task_id, executor_agent=executor.agent_id
            )
        except ValidationError as exc:
            self.audit.log(verifier.agent_id, "verification.invalid_report", subject=task.uid,
                           payload={"error": exc.message})
            return None

    def _select_verifier(self, executor: AgentRecord) -> AgentRecord | None:
        if not self.config.require_independent_verifier:
            return None
        configured = self.config.verifier_agent_id
        if configured:
            agent = self.store.get_agent(configured)
            if agent and agent.agent_id != executor.agent_id:
                return agent
        candidates = [
            a for a in self.store.list_agents()
            if a.agent_id != executor.agent_id and a.status != "offline"
            and a.adapter_type in ("cli", "opencode")
        ]
        return candidates[0] if candidates else None

    # ------------------------------------------------------------------
    # Waiting-user resume / result import
    # ------------------------------------------------------------------
    def submit_result(self, uid: str, payload: dict[str, Any] | str | Path) -> dict[str, Any]:
        task = self.store.get_task(uid)
        if not task:
            raise NotFoundError(f"任务不存在: {uid}")
        if isinstance(payload, (str, Path)):
            data = read_json(payload)
            if data is None:
                try:
                    data = json.loads(str(payload))
                except json.JSONDecodeError:
                    data = None
        else:
            data = payload
        if not isinstance(data, dict):
            raise ValidationError("成果包必须是 JSON 对象")
        try:
            result = ResultPackage.validate(
                data, expected_task_id=task.task_id,
                expected_agent_id=task.assigned_agent or str(data.get("agent_id", "")),
            )
        except ValidationError as exc:
            self.store.save_result(task, ResultPackage(data), valid=False, error=exc.message)
            self.audit.log("user", "result.rejected", subject=uid, payload={"error": exc.message})
            raise
        persistent = Path(task.task_dir) / "result.json" if task.task_dir else self.paths.task_dir(uid) / "result.json"
        write_json(persistent, data)
        self.store.save_result(task, result, valid=True)
        self.audit.log("user", "result.submitted", subject=uid,
                       payload={"status": result.status, "summary": result.summary[:300]})
        if task.status in (TaskStatus.WAITING_USER.value, TaskStatus.RUNNING.value,
                           TaskStatus.WAITING_AGENT.value, TaskStatus.VERIFYING.value):
            self.store.set_task_status(uid, TaskStatus.VERIFYING.value, force=True)
            agent = self.store.get_agent(task.assigned_agent) if task.assigned_agent else None
            if agent:
                self._ensure_change(task, agent, result)
                self._finish_verification(task, agent, result)
            else:
                self.store.set_task_status(uid, TaskStatus.FAILED.value,
                                           error="成果包对应的执行 Agent 不存在", force=True)
        elif task.status == TaskStatus.PAUSED.value:
            self.audit.log("control-plane", "result.stored_paused", subject=uid)
        return {"status": task.status, "task_uid": uid, "result": result.payload}

    def resume(self, run_id: str, *, max_parallel: int | None = None, background: bool = False) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise NotFoundError(f"运行不存在: {run_id}")
        if run.get("status") == "canceled":
            raise ValidationError("运行已被用户停止；请创建新的运行")
        if run.get("goal_status") == "blocked":
            round_number = int(run.get("round_number") or 1)
            summary = dict(run.get("summary") or {})
            history = [dict(item) for item in (summary.get("rounds") or [])
                       if isinstance(item, dict)]
            if history:
                history[-1]["blocked_streak"] = 0
                history[-1]["resumed_at"] = now_iso()
            summary["rounds"] = history
            summary["goal_status"] = "active"
            summary["blocked_reason"] = ""
            new_max = round_number + max(1, int(self.config.max_rounds))
            self.store.update_run(
                run_id, goal_status="active", blocked_reason="", error="",
                max_rounds=new_max, summary=summary, status="ready", finished_at="",
            )
            self.audit.log("user", "round.resumed", subject=run_id,
                           payload={"from_round": round_number,
                                    "max_rounds": new_max,
                                    "added_rounds": int(self.config.max_rounds)})
            run = self.store.get_run(run_id) or run
        tasks = self.store.list_tasks(run_id)
        requeued = 0
        for task in tasks:
            if task.status not in (TaskStatus.WAITING_USER.value, TaskStatus.BLOCKED.value,
                                   TaskStatus.PAUSED.value, TaskStatus.FAILED.value):
                continue
            if self._is_active(task.uid):
                # The adapter is still alive and may be waiting for the user to
                # click send inside the app. Requeueing now would double-run it.
                continue
            latest = self._latest_task_approval(task)
            pending = self._task_blocking_approval(task)
            if pending:
                continue
            result = self.store.get_result(task.uid)
            if result and result["valid"]:
                agent = self.store.get_agent(task.assigned_agent or "")
                if agent:
                    self.store.set_task_status(task.uid, TaskStatus.VERIFYING.value, force=True)
                    self._ensure_change(task, agent, ResultPackage(result["payload"]))
                    self._finish_verification(task, agent, ResultPackage(result["payload"]))
                continue
            if task.status == TaskStatus.WAITING_USER.value:
                waiting_for_approval = any(
                    marker in (task.error or "")
                    for marker in ("审批", "预算", "budget", "approval")
                )
                approved_after_wait = waiting_for_approval and latest \
                    and latest.status == "approved"
                manual_restart = (task.error or "").startswith("MANUAL_ACTION_REQUIRED")
                if approved_after_wait or manual_restart:
                    try:
                        (Path(task.task_dir) / "manual-action.json").unlink()
                    except (FileNotFoundError, OSError):
                        pass
                    self.store.set_task_status(task.uid, TaskStatus.QUEUED.value, force=True)
                    self.store.update_task(task.uid, error="")
                    requeued += 1
                    self.audit.log(
                        "control-plane",
                        "task.requeued_after_approval" if approved_after_wait
                        else "task.requeued_after_manual_action",
                        subject=task.uid,
                        payload={"approval": latest.approval_id if latest else "",
                                 "kind": latest.kind if latest else ""})
            elif task.status == TaskStatus.FAILED.value:
                self.store.set_task_status(task.uid, TaskStatus.QUEUED.value, force=True)
                requeued += 1
            elif task.status == TaskStatus.PAUSED.value:
                self.store.set_task_status(task.uid, TaskStatus.QUEUED.value, force=True)
                self.store.update_task(task.uid, error="")
                requeued += 1
            elif task.status == TaskStatus.BLOCKED.value and not self.leases.conflicts(self.project.project_id, task):
                self.store.set_task_status(task.uid, TaskStatus.QUEUED.value, force=True)
                requeued += 1
        self.audit.log("user", "run.resumed", subject=run_id, payload={"requeued": requeued})
        return self.start(run_id, max_parallel=max_parallel, background=background)

    def _latest_task_approval(self, task: Task):
        approvals = [a for a in self.approvals.all(limit=1000) if a.task_uid == task.uid]
        approvals.sort(key=lambda a: a.requested_at, reverse=True)
        return approvals[0] if approvals else None

    def _task_blocking_approval(self, task: Task):
        approvals = [a for a in self.approvals.all(limit=200) if a.task_uid == task.uid and a.status == "pending"]
        return approvals[0] if approvals else None

    # ------------------------------------------------------------------
    # Summary and finalization
    # ------------------------------------------------------------------
    def _finalize_run(self, run_id: str, plan: TaskPlan) -> dict[str, Any]:
        tasks = self.store.list_tasks(run_id)
        # After a failed task, queued descendants must become blocked before
        # deciding the run is merely "paused".  Otherwise a round with a
        # failure and blocked dependents would never reach summary/judgement.
        self._block_dependent_on_failures(tasks)
        tasks = self.store.list_tasks(run_id)
        waiting = [t for t in tasks if t.status in (TaskStatus.WAITING_USER.value, TaskStatus.WAITING_AGENT.value)]
        failed = [t for t in tasks if t.status in (TaskStatus.FAILED.value, TaskStatus.BLOCKED.value)]
        if waiting:
            self.store.update_run(run_id, status="waiting_user")
            self.checkpoints.create("run_waiting_user", run_id=run_id,
                                    extra={"waiting": len(waiting)})
            self.audit.log("control-plane", "run.waiting_user", subject=run_id,
                           payload={"tasks": [t.task_id for t in waiting]})
            self.memory.sync_workflow(run_id)
            return {"status": "waiting_user", "run_id": run_id,
                    "waiting": [t.task_id for t in waiting], "failed": [t.task_id for t in failed]}
        if any(t.status != TaskStatus.COMPLETED.value and t.status not in
               (TaskStatus.FAILED.value, TaskStatus.BLOCKED.value) for t in tasks):
            self.store.update_run(run_id, status="paused")
            self.memory.sync_workflow(run_id)
            return {"status": "paused", "run_id": run_id}
        if failed and not self.config.multi_round:
            self.store.update_run(run_id, status="failed", finished_at=now_iso(),
                                  error="存在失败任务")
            self.audit.log("control-plane", "run.failed", subject=run_id,
                           payload={"failed": [t.task_id for t in failed]})
            self.checkpoints.create("run_failed", run_id=run_id)
            self.memory.sync_workflow(run_id)
            return {"status": "failed", "run_id": run_id,
                    "failed": [t.task_id for t in failed]}

        # Even a round with failed tasks gets the main node's judgement: it can
        # reject and hand concrete next actions to the next round.  A model
        # "approve" cannot complete the run while failures remain.
        return self._request_summary(run_id, plan)

    def _request_summary(self, run_id: str, plan: TaskPlan) -> dict[str, Any]:
        run = self.store.get_run(run_id) or {}
        tasks = self.store.list_tasks(run_id)
        changes = [c.to_dict() for c in self.store.list_changes(run_id=run_id)]
        verifications = {t.uid: (self.store.latest_verification(t.uid) or {}) for t in tasks}
        ctx = self.context.summary_context(
            run=run, plan=plan.to_dict(), tasks=tasks,
            verification_by_task=verifications, changes=changes,
        )
        ctx["round"] = self._summary_round_context(run, tasks, plan)
        if self.cost:
            ctx = self.cost.compress_context(
                ctx, self.config.context_summary_tokens,
                kind="context_compression", run_id=run_id,
            )
        main_agent = self.store.get_agent(run.get("main_agent_id") or plan.main_agent_id)
        if not main_agent:
            self.store.update_run(run_id, status="failed", finished_at=now_iso(),
                                  error="主节点 Agent 不存在")
            return {"status": "failed", "run_id": run_id, "error": "主节点 Agent 不存在"}
        summary_workspace = self._ensure_run_workspace(run_id, run.get("base_commit", ""))
        gui_mode = str(main_agent.adapter_type) == "gui_automation"
        canonical_summary = self.paths.run_dir(run_id) / "summary" / "summary.json"
        agent_summary_path = (Path(summary_workspace) / "summary.json"
                              if gui_mode else canonical_summary)
        prompt_path, summary_path = self.prompts.write_summary_request(
            run_id=run_id, main_agent=main_agent, model=run.get("model", ""), context=ctx,
            output_path=agent_summary_path, workspace=summary_workspace, gui_mode=gui_mode,
        )
        if self.bus:
            self.bus.publish("prompt", {
                "purpose": "summary", "run_id": run_id, "agent_id": main_agent.agent_id,
                "prompt_path": str(prompt_path), "context_tokens": self._estimate(ctx),
                "preview": read_text(prompt_path, default="")[:1200],
            }, source=main_agent.agent_id, correlation_id=run_id, sync_level=5)
        synthetic = Task(
            uid=f"{run_id}:SUMMARY", project_id=self.project.project_id,
            plan_id=plan.plan_id, run_id=run_id,
            # A summary is a main-node activity, not plan task #1.  Reusing
            # plan.tasks[0] made the WebUI report a T001 manual action while
            # Trae was actually running the summary.
            spec=TaskSpec(task_id="SUMMARY", title="主节点汇总",
                          objective="汇总本轮任务结果并给出完成判定",
                          role="summarize", model=run.get("model", ""),
                          type="review", risk_level="low", approval_policy="auto"),
            status=TaskStatus.RUNNING.value, workdir=summary_workspace,
            task_dir=str(prompt_path.parent), base_commit=run.get("base_commit", ""),
        )
        request = ExecutionRequest(
            project_id=self.project.project_id, project_root=self.project.root,
            task=synthetic, workdir=summary_workspace, task_dir=str(prompt_path.parent),
            prompt_path=str(prompt_path), result_path=str(agent_summary_path),
            model=run.get("model", ""), timeout_seconds=self.config.summary_timeout_seconds,
            purpose="summary", full_control=bool(self.config.full_control),
        )
        activity_path = Path(prompt_path.parent) / "activity.json"
        activity = {
            "phase": "summary", "round": int(run.get("round_number") or 1),
            "run_id": run_id, "task_id": "SUMMARY",
            "agent_id": main_agent.agent_id, "agent_name": main_agent.name,
            "model": request.model, "status": "running",
            "started_at": now_iso(), "prompt_path": str(prompt_path),
            "workspace": summary_workspace,
        }
        write_json(activity_path, activity)
        outcome = create_adapter(main_agent).execute(request)
        activity.update({
            "status": outcome.status, "error": outcome.error[:500],
            "finished_at": now_iso(), "duration_ms": outcome.duration_ms,
        })
        write_json(activity_path, activity)
        self.budget.record(
            synthetic, main_agent.agent_id, request.model, outcome.usage,
            duration_ms=outcome.duration_ms, run_id=run_id, status=outcome.status,
        )
        if self.bus:
            self.bus.publish("response", {
                "purpose": "summary", "run_id": run_id, "agent_id": main_agent.agent_id,
                "status": outcome.status, "error": outcome.error[:500],
                "usage": outcome.usage.to_dict(), "summary_path": str(summary_path),
            }, source=main_agent.agent_id, correlation_id=run_id, sync_level=5)
        if outcome.status in ("waiting_user", "waiting_agent"):
            self.store.update_run(run_id, status="waiting_user")
            self.checkpoints.create("summary_waiting", run_id=run_id,
                                    extra={"prompt": str(prompt_path), "summary_path": str(summary_path)})
            return {"status": "waiting_user", "run_id": run_id, "kind": "summary",
                    "prompt_path": str(prompt_path), "summary_path": str(summary_path)}
        data = outcome.result_payload if outcome.result_payload is not None else read_json(agent_summary_path)
        if isinstance(data, dict):
            write_json(summary_path, data)
        if data is None:
            return self._fallback_after_missing_summary(run_id, plan, tasks, outcome)
        return self.finalize_summary(run_id, data)

    def _fallback_after_missing_summary(self, run_id: str, plan: TaskPlan,
                                        tasks: list[Task], outcome: Any) -> dict[str, Any]:
        """Do not dead-end when the main GUI agent cannot produce summary.json.

        A missing summary with recorded task/verification failures is treated
        as a reject with evidence-derived next actions, so the next round can
        still start.  A clean task graph with a missing summary becomes a
        summary-unavailable blocker for the user, never a silent success.
        """
        reason = f"主节点未输出汇总: {getattr(outcome, 'error', '') or getattr(outcome, 'status', '')}"
        results = self._round_results(tasks)
        run = self.store.get_run(run_id) or {}
        round_number = int(run.get("round_number") or 1)
        max_rounds = int(run.get("max_rounds") or self.config.max_rounds or 1)
        current = round_tasks_for(tasks, round_number)
        has_failure = any(
            t.status in (TaskStatus.FAILED.value, TaskStatus.BLOCKED.value)
            or t.verification_status == VerificationStatus.FAILED.value
            for t in current
        )
        if not self.config.multi_round:
            self.store.update_run(run_id, status="failed", finished_at=now_iso(),
                                  error=reason)
            return {"status": "failed", "run_id": run_id, "error": reason}
        unresolved = collect_unresolved(
            tasks,
            {"merge_recommendation": "reject", "completion": reason,
             "next_actions": [], "risks": []},
            results,
        )
        if has_failure:
            actions = [item for item in unresolved
                       if not item.startswith("主节点判定未通过")] or [reason]
            data = {
                "summary_version": 1,
                "completion": reason,
                "changes": [],
                "verification": "存在失败任务或验证失败",
                "risks": actions[:12],
                "merge_recommendation": "reject",
                "next_actions": actions[:12],
                "goal_status": "continue",
            }
            self.audit.log("control-plane", "summary.fallback_continue", subject=run_id,
                           payload={"reason": reason, "actions": actions[:12]})
            return self.finalize_summary(run_id, data)

        decision = RoundDecision(
            action="blocked", reason=reason, code="summary-unavailable",
            goal_status="blocked", unresolved=[reason],
            signature=signature_of([reason]), blocked_streak=1,
            round_number=round_number, max_rounds=max_rounds,
        )
        data = {
            "summary_version": 1,
            "completion": "任务已执行完毕，但主节点未输出汇总，无法完成语义验收",
            "changes": [],
            "verification": "任务侧验证已结束；缺少主节点汇总",
            "risks": [reason],
            "merge_recommendation": "reject",
            "next_actions": ["恢复主节点后重新汇总"],
            "goal_status": "blocked",
            "blocking_reason": reason,
        }
        summary = self._persist_round(run_id, run, data, decision, results)
        blocked = f"summary-unavailable: {reason}"
        self.store.update_run(run_id, status="waiting_user", goal_status="blocked",
                              blocked_reason=blocked, error=blocked, summary=summary,
                              finished_at=now_iso())
        self.checkpoints.create("round_blocked", run_id=run_id,
                                extra={"round": round_number, "code": "summary-unavailable"})
        self.memory.sync_workflow(run_id)
        return {"status": "waiting_user", "run_id": run_id, "round": round_number,
                "goal_status": "blocked", "blocked_reason": blocked,
                "decision": decision.to_dict(), "summary": summary}

    def submit_summary(self, run_id: str, payload: dict[str, Any] | str | Path) -> dict[str, Any]:
        data = read_json(payload) if isinstance(payload, (str, Path)) else payload
        if not isinstance(data, dict):
            raise ValidationError("汇总必须是 JSON 对象")
        return self.finalize_summary(run_id, data, auto_start=True)

    def finalize_summary(self, run_id: str, data: dict[str, Any], *,
                         auto_start: bool = False) -> dict[str, Any]:
        required = ("summary_version", "completion", "changes", "verification", "risks",
                    "merge_recommendation", "next_actions")
        missing = [k for k in required if k not in data]
        if missing:
            raise ValidationError(f"汇总缺少字段: {missing}")
        if data.get("merge_recommendation") not in ("approve", "reject"):
            raise ValidationError("merge_recommendation 必须是 approve/reject")
        for key in ("changes", "risks", "next_actions"):
            if not isinstance(data.get(key), list):
                raise ValidationError(f"汇总 {key} 必须是数组")
        declared = str(data.get("goal_status") or "").lower()
        if declared and declared not in ("complete", "continue", "blocked"):
            raise ValidationError("goal_status 必须是 complete/continue/blocked")
        if data.get("blocking_reason") is not None \
                and not isinstance(data.get("blocking_reason"), str):
            raise ValidationError("blocking_reason 必须是字符串")
        run = self.store.get_run(run_id)
        if not run:
            raise NotFoundError(f"运行不存在: {run_id}")
        tasks = self.store.list_tasks(run_id)
        plan = self.store.get_plan(run["plan_id"])
        if not plan:
            raise NotFoundError(f"计划不存在: {run['plan_id']}")
        results = self._round_results(tasks)
        round_number = int(run.get("round_number") or 1)
        max_rounds = int(run.get("max_rounds") or self.config.max_rounds or 1)
        history = self._round_history(run)
        if self.config.multi_round:
            decision = decide_round(
                tasks=tasks, summary=data, round_number=round_number,
                max_rounds=max_rounds,
                blocked_after_rounds=self.config.blocked_after_rounds,
                history=history, results=results,
            )
        else:
            unresolved = collect_unresolved(tasks, data, results)
            recommendation = str(data.get("merge_recommendation") or "").lower()
            all_completed = bool(tasks) and all(
                t.status == TaskStatus.COMPLETED.value for t in tasks
            )
            verification_failed = any(
                t.verification_status == VerificationStatus.FAILED.value for t in tasks
            )
            complete = all_completed and not verification_failed and recommendation == "approve"
            decision = RoundDecision(
                action="complete" if complete else "await_user",
                reason="多轮续跑已关闭" if not complete else "主节点批准且全部任务完成",
                goal_status="complete" if complete else "awaiting_user",
                unresolved=unresolved, signature=signature_of(unresolved),
                blocked_streak=1, round_number=round_number, max_rounds=max_rounds,
            )
        summary = self._persist_round(run_id, run, data, decision, results)

        if decision.action == "continue":
            result = self._prepare_next_round(run_id, run, plan, summary, decision)
            if result.get("status") == "round_continuing" and auto_start:
                self.start(run_id, background=True)
            return result

        if decision.action == "blocked":
            reason = f"{decision.code}: {decision.reason}"
            self.store.update_run(
                run_id, status="waiting_user", finished_at=now_iso(),
                goal_status="blocked", blocked_reason=reason, error=reason,
            )
            self.checkpoints.create("round_blocked", run_id=run_id,
                                    extra={"round": round_number, "code": decision.code})
            self.audit.log("control-plane", "round.blocked", subject=run_id,
                           payload={"round": round_number, "code": decision.code,
                                    "reason": decision.reason,
                                    "unresolved": decision.unresolved[:12]})
            self.memory.sync_workflow(run_id)
            return {"status": "waiting_user", "run_id": run_id,
                    "round": round_number, "goal_status": "blocked",
                    "blocked_reason": reason, "decision": decision.to_dict(),
                    "summary": summary}

        patches = [
            {"task_id": t.task_id, "agent": t.assigned_agent, "patch": t.patch_path,
             "verification": t.verification_status}
            for t in tasks if t.patch_path
        ]
        if self.governance:
            self.store.update_run(run_id, status="pending_acceptance", summary=summary,
                                  finished_at=now_iso(),
                                  goal_status=decision.goal_status, blocked_reason="")
            approval = self.governance.ensure_acceptance_approval(run_id, plan, summary, tasks)
            status = "pending_acceptance"
            checkpoint_kind = "before_acceptance"
        else:
            self.store.update_run(run_id, status="pending_merge", summary=summary,
                                  finished_at=now_iso(),
                                  goal_status=decision.goal_status, blocked_reason="")
            approval = self.approvals.ensure_merge_approval(
                run_id, plan.goal, patches, summary,
            )
            status = "pending_merge"
            checkpoint_kind = "before_merge"
        self.checkpoints.create(checkpoint_kind, run_id=run_id,
                                extra={"approval": approval.approval_id,
                                       "patches": len(patches),
                                       "round": round_number})
        self.audit.log("control-plane", "run.summary", subject=run_id,
                       payload={"recommendation": data["merge_recommendation"],
                                "approval": approval.approval_id, "status": status,
                                "round": round_number,
                                "goal_status": decision.goal_status})
        self.memory.record_decision(
            f"主节点第 {round_number} 轮汇总：{data.get('completion', '')[:300]}",
            agent_id=run.get("main_agent_id", ""),
            model=run.get("model", ""),
            approved=decision.action == "complete",
        )
        self.memory.sync_workflow(run_id)
        return {"status": status, "run_id": run_id,
                "approval_id": approval.approval_id, "summary": summary,
                "round": round_number, "goal_status": decision.goal_status,
                "decision": decision.to_dict()}

    # ------------------------------------------------------------------
    # Multi-round helpers
    # ------------------------------------------------------------------
    def _round_results(self, tasks: list[Task]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for task in tasks:
            row = self.store.get_result(task.uid)
            payload = (row or {}).get("payload")
            if isinstance(payload, dict):
                out[task.uid] = payload
        return out

    def _round_history(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        summary = run.get("summary") or {}
        history = summary.get("rounds") if isinstance(summary, dict) else []
        return [dict(item) for item in (history or []) if isinstance(item, dict)]

    def _summary_round_context(self, run: dict[str, Any], tasks: list[Task],
                               plan: TaskPlan) -> dict[str, Any]:
        return {
            "round": int(run.get("round_number") or 1),
            "max_rounds": int(run.get("max_rounds") or self.config.max_rounds or 1),
            "blocked_after_rounds": int(self.config.blocked_after_rounds),
            "goal_status": run.get("goal_status") or "active",
            "previous_rounds": self._round_history(run)[-3:],
            "instruction": (
                "本轮未完成时必须 merge_recommendation=reject、goal_status=continue，"
                "并把可执行修复项写入 next_actions；只有目标真正达成才 complete。"
            ),
        }

    def _task_round_context(self, run: dict[str, Any], task: Task) -> dict[str, Any]:
        round_number = int(run.get("round_number") or 1)
        if round_number <= 1:
            return {}
        return {
            "round": round_number,
            "max_rounds": int(run.get("max_rounds") or self.config.max_rounds or 1),
            "previous_rounds": self._round_history(run)[-2:],
            "instruction": "工作区已包含上一轮依赖补丁；只关闭列出未解决项，不要重做已完成工作。",
        }

    def _persist_round(self, run_id: str, run: dict[str, Any], data: dict[str, Any],
                       decision: RoundDecision,
                       results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        round_number = int(decision.round_number or 1)
        history = [item for item in self._round_history(run)
                   if int(item.get("round") or 0) != round_number]
        entry = {
            "round": round_number,
            "completion": data.get("completion", ""),
            "verification": data.get("verification", ""),
            "merge_recommendation": data.get("merge_recommendation", ""),
            "goal_status": decision.goal_status,
            "risks": data.get("risks", []),
            "next_actions": data.get("next_actions", []),
            "unresolved": decision.unresolved,
            "signature": decision.signature,
            "blocked_streak": decision.blocked_streak,
            "action": decision.action,
            "code": decision.code,
            "reason": decision.reason,
            "finished_at": now_iso(),
        }
        summary = dict(data)
        summary["round"] = round_number
        summary["max_rounds"] = int(decision.max_rounds or 0)
        summary["goal_status"] = decision.goal_status
        summary["rounds"] = history + [entry]
        summary["blocked_reason"] = (
            f"{decision.code}: {decision.reason}" if decision.action == "blocked" else ""
        )
        self.store.update_run(run_id, summary=summary)
        rounds_dir = ensure_dir(self.paths.run_dir(run_id) / "rounds")
        write_json(rounds_dir / f"round-{round_number}.json", {
            "round": entry, "summary": data, "decision": decision.to_dict(),
        })
        action_name = {
            "complete": "round.completed", "continue": "round.continued",
            "blocked": "round.blocked", "await_user": "round.awaiting_user",
        }.get(decision.action, "round.finished")
        self.audit.log("control-plane", action_name, subject=run_id,
                       payload={"round": round_number,
                                "action": decision.action,
                                "goal_status": decision.goal_status,
                                "code": decision.code,
                                "signature": decision.signature,
                                "blocked_streak": decision.blocked_streak,
                                "unresolved_count": len(decision.unresolved)})
        if self.bus:
            self.bus.publish("workflow", {
                "phase": action_name, "run_id": run_id, "round": round_number,
                "max_rounds": int(decision.max_rounds or 0),
                "goal_status": decision.goal_status,
                "signature": decision.signature,
                "unresolved": decision.unresolved[:20],
            }, source="control-plane", correlation_id=run_id, sync_level=2)
        self.memory.record_decision(
            f"第 {round_number} 轮结果：{decision.action}，"
            f"{data.get('completion', '')[:240]}",
            agent_id=run.get("main_agent_id", ""), model=run.get("model", ""),
            approved=decision.action == "complete",
        )
        return summary

    def _continuation_context(self, run: dict[str, Any], plan: TaskPlan,
                              tasks: list[Task], summary: dict[str, Any],
                              decision: RoundDecision, new_round: int,
                              max_rounds: int) -> dict[str, Any]:
        failed = [
            {"task_id": t.task_id, "status": t.status, "error": t.error}
            for t in tasks if t.status in (TaskStatus.FAILED.value, TaskStatus.BLOCKED.value)
        ]
        verification = [
            {"task_id": t.task_id, "status": t.verification_status}
            for t in tasks if t.verification_status == VerificationStatus.FAILED.value
        ]
        history = [item for item in (summary.get("rounds") or [])
                   if isinstance(item, dict)][-3:]
        return {
            "round": new_round,
            "max_rounds": max_rounds,
            "original_goal": plan.goal,
            "previous_round": int(run.get("round_number") or new_round - 1),
            "previous_completion": summary.get("completion", ""),
            "previous_verification": summary.get("verification", ""),
            "previous_risks": summary.get("risks", []),
            "previous_next_actions": summary.get("next_actions", []),
            "unresolved": decision.unresolved,
            "failed_tasks": failed,
            "verification_failures": verification,
            "completed_tasks": [
                t.task_id for t in tasks if t.status == TaskStatus.COMPLETED.value
            ],
            "round_history": history,
            "instruction": (
                "只规划关闭上述未解决项所需的最小增量任务；不要重复已完成工作。"
                "可写任务必须给出最小必要 scope，并依赖上一轮相关任务；"
                "不要把难度或不确定性写成阻塞。"
            ),
        }

    def _prepare_next_round(self, run_id: str, run: dict[str, Any], plan: TaskPlan,
                            summary: dict[str, Any],
                            decision: RoundDecision) -> dict[str, Any]:
        tasks = self.store.list_tasks(run_id)
        new_round = int(run.get("round_number") or 1) + 1
        max_rounds = int(run.get("max_rounds") or self.config.max_rounds or 1)
        context = self._continuation_context(
            run, plan, tasks, summary, decision, new_round, max_rounds,
        )
        notes: list[str] = []
        delta_specs: list[TaskSpec] = []
        if self.config.round_replan and self.planner:
            try:
                delta = self.planner.request_round_plan(
                    goal=plan.goal, base_plan=plan,
                    round_number=new_round, round_context=context,
                )
            except Exception as exc:
                delta = {"status": "failed", "error": str(exc)[:500]}
            if delta.get("status") == "completed":
                try:
                    delta_specs = prefix_delta_specs(
                        specs=delta["plan"].tasks, round_number=new_round,
                        tasks=tasks, plan=plan,
                    ) or []
                except Exception as exc:
                    notes.append(f"主节点增量计划 ID/依赖改写失败: {str(exc)[:300]}")
                    delta_specs = []
                if not delta_specs:
                    notes.append("主节点增量计划没有可写任务或依赖非法，使用回退任务")
            else:
                notes.append(
                    "主节点增量规划失败，使用回退任务: "
                    + str(delta.get("error") or delta.get("status"))[:300]
                )
        else:
            notes.append("未启用主节点增量规划，使用回退任务")

        candidates: list[tuple[str, list[TaskSpec]]] = []
        if delta_specs:
            candidates.append(("main_agent", delta_specs))
        try:
            fallback = fallback_specs(
                plan=plan, tasks=tasks, summary=summary, decision=decision,
                round_number=new_round,
                default_token_budget=self.config.default_task_token_budget,
                default_time_limit_minutes=30,
                default_retry_limit=self.config.max_retries_default,
                test_command=self.project.test_command,
            )
            candidates.append(("fallback", fallback))
        except Exception as exc:
            notes.append(f"回退任务生成失败: {str(exc)[:300]}")
        chosen: tuple[str, list[TaskSpec], TaskPlan] | None = None
        for source, specs in candidates:
            trial = TaskPlan(
                plan_id=plan.plan_id, project_id=plan.project_id, goal=plan.goal,
                tasks=[*plan.tasks, *specs],
                constraints=list(plan.constraints), requirements=list(plan.requirements),
                nonfunctional=list(plan.nonfunctional), risks=list(plan.risks),
                open_questions=list(plan.open_questions), main_agent_id=plan.main_agent_id,
                model=plan.model, status=plan.status, created_at=plan.created_at,
                raw=plan.raw,
            )
            try:
                trial.validate()
            except ValidationError as exc:
                notes.append(f"{source} 增量计划合并校验失败: {exc.message}")
                continue
            chosen = (source, specs, trial)
            break
        if chosen is None:
            reason = "round-planning-failed: 主节点增量计划与回退计划均无法通过校验"
            summary = dict(summary)
            summary["goal_status"] = "blocked"
            summary["blocked_reason"] = reason
            self.store.update_run(
                run_id, status="waiting_user", goal_status="blocked",
                blocked_reason=reason, error=reason, finished_at=now_iso(),
                summary=summary,
            )
            self.audit.log("control-plane", "round.planning_failed", subject=run_id,
                           payload={"round": new_round, "notes": notes})
            return {"status": "waiting_user", "run_id": run_id, "round": new_round,
                    "goal_status": "blocked", "blocked_reason": reason,
                    "notes": notes, "decision": decision.to_dict(), "summary": summary}

        source, specs, candidate = chosen
        self.store.save_plan(candidate)
        self.store.create_tasks(run_id, candidate, run["base_commit"], specs=specs)
        self.store.update_run(
            run_id, status="ready", round_number=new_round, max_rounds=max_rounds,
            goal_status="active", blocked_reason="", error="", finished_at="",
        )
        self.checkpoints.create(f"round_{new_round}_planned", run_id=run_id,
                                extra={"round": new_round, "source": source,
                                       "tasks": [s.task_id for s in specs]})
        self.audit.log("control-plane", "round.plan_appended", subject=run_id,
                       payload={"round": new_round, "source": source,
                                "tasks": [s.task_id for s in specs],
                                "unresolved": decision.unresolved[:12],
                                "notes": notes})
        if self.bus:
            self.bus.publish("workflow", {
                "phase": "round_planned", "run_id": run_id, "round": new_round,
                "source": source, "tasks": [s.task_id for s in specs],
                "unresolved": decision.unresolved[:20],
            }, source="control-plane", correlation_id=run_id, sync_level=2)
        self.memory.sync_workflow(run_id)
        self.memory.sync_task_state(run_id)
        return {
            "status": "round_continuing", "run_id": run_id, "round": new_round,
            "rounds_total": max_rounds, "plan_source": source,
            "planned_tasks": [s.task_id for s in specs],
            "unresolved": decision.unresolved, "notes": notes,
            "summary": summary, "decision": decision.to_dict(),
        }


    # ------------------------------------------------------------------
    # Status / control
    # ------------------------------------------------------------------
    def status(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise NotFoundError(f"运行不存在: {run_id}")
        tasks = self.store.list_tasks(run_id)
        approvals = [a.to_dict() for a in self.approvals.all(limit=200)
                     if a.run_id == run_id or a.plan_id == run["plan_id"]]
        return {
            "run": run,
            "tasks": [t.to_dict() for t in tasks],
            "approvals": approvals,
            "budget": self.budget.snapshot(run_id=run_id),
            "checkpoint": self.checkpoints.latest(run_id=run_id),
        }

    def cancel_task(self, uid: str, *, by: str = "user") -> dict[str, Any]:
        task = self.store.get_task(uid)
        if not task:
            raise NotFoundError(f"任务不存在: {uid}")
        if task.pid:
            from ..adapters.process import kill_tree
            kill_tree(task.pid)
        self.store.set_task_status(uid, TaskStatus.CANCELED.value, force=True)
        self.leases.release(uid)
        self.audit.log(by, "task.canceled", subject=uid)
        return {"status": "canceled", "task_uid": uid}

    def retry_task(self, uid: str, *, by: str = "user") -> dict[str, Any]:
        task = self.store.get_task(uid)
        if not task:
            raise NotFoundError(f"任务不存在: {uid}")
        if self._is_active(uid):
            raise ConflictError("该节点仍在等待 Agent 完成；请先在应用中提交/审批，"
                                "或先暂停运行再重试")
        try:
            (Path(task.task_dir) / "manual-action.json").unlink()
        except (FileNotFoundError, OSError):
            pass
        self.store.set_task_status(uid, TaskStatus.QUEUED.value, force=True)
        self.store.update_task(uid, error="")
        self.audit.log(by, "task.retry_requested", subject=uid)
        return {"status": "queued", "task_uid": uid}

    def reverify_task(self, uid: str, *, by: str = "user") -> dict[str, Any]:
        """Re-run control-plane verification on an existing result, no Agent run.

        Useful when a plan/verification-command bug failed a task whose result
        and patch are already complete; it must not re-open the Agent session.
        """
        task = self.store.get_task(uid)
        if not task:
            raise NotFoundError(f"任务不存在: {uid}")
        if self._is_active(uid):
            raise ConflictError("该节点仍在执行，不能只重新验证")
        candidates = [
            Path(task.task_dir) / "result.json" if task.task_dir else Path(),
            Path(task.result_path) if task.result_path else Path(),
        ]
        data: dict[str, Any] = {}
        for path in candidates:
            if not path or not path.exists():
                continue
            loaded = read_json(path, {})
            if isinstance(loaded, dict) and loaded:
                data = loaded
                break
        if not data:
            raise ValidationError("没有可复用的 result.json，请重试节点")
        agent = self.store.get_agent(task.assigned_agent or "")
        if agent is None:
            raise ValidationError("任务没有可用的执行 Agent 记录")
        normalized, normalized_fields = ResultPackage.normalize(data)
        if normalized_fields:
            self.audit.log("control-plane", "result.normalized", subject=uid,
                           payload={"fields": normalized_fields, "source": "reverify"})
        result = ResultPackage.validate(
            normalized, expected_task_id=task.task_id,
            expected_agent_id=agent.agent_id,
        )
        self.store.save_result(task, result, valid=True)
        self.store.set_task_status(uid, TaskStatus.VERIFYING.value, force=True)
        self.audit.log(by, "task.reverify_requested", subject=uid)
        self._ensure_change(task, agent, result)
        self._finish_verification(task, agent, result)
        refreshed = self.store.get_task(uid)
        return {"status": refreshed.status if refreshed else task.status,
                "task_uid": uid,
                "verification_status": refreshed.verification_status if refreshed else "",
                "error": (refreshed.error if refreshed else "")[:500]}
