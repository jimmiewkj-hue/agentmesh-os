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
    LeaseManager, create_worktree, generate_patch, patch_files, run_git,
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
from .paths import WorkspacePaths
from .prompts import PromptBuilder
from .router import CapabilityRouter
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
        self.verifier = verifier
        self.bus = bus
        self.router = router
        self.cost = cost
        self.governance = governance
        self.scheduler = TaskScheduler(config)
        self.prompts = PromptBuilder(paths, project)
        self.leases = LeaseManager(store)
        self._wt_lock = threading.Lock()
        self._run_locks: dict[str, threading.Lock] = {}
        self._futures: dict[str, Future] = {}
        self._active_run_id = ""

    # ------------------------------------------------------------------
    # Start / loop
    # ------------------------------------------------------------------
    def start(self, run_id: str, *, max_parallel: int | None = None, background: bool = False) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise NotFoundError(f"运行不存在: {run_id}")
        if run["status"] in ("completed", "pending_acceptance", "pending_merge", "released"):
            return {"status": run["status"], "run_id": run_id,
                    "message": "运行已结束或被治理门禁阻塞，请按审批流程处理"}
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
        run = self.store.get_run(run_id)
        plan = self.store.get_plan(run["plan_id"])
        if not plan:
            raise NotFoundError(f"计划不存在: {run['plan_id']}")
        self.store.update_run(run_id, status="running", started_at=run["started_at"] or now_iso())
        run_workspace = self._ensure_run_workspace(run_id, run["base_commit"])
        self.audit.log("control-plane", "run.started", subject=run_id,
                       payload={"max_parallel": max_parallel or self.config.max_parallel,
                                "workspace": run_workspace})
        self.checkpoints.create("run_started", run_id=run_id)
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
                if live.get("status") == "paused":
                    for task in self.store.list_tasks(run_id):
                        if task.status in (TaskStatus.QUEUED.value, TaskStatus.READY.value):
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
                                                    TaskStatus.REJECTED.value):
                    blocked = True
                    break
            if blocked:
                self.store.set_task_status(task.uid, TaskStatus.BLOCKED.value,
                                           error="前置任务失败/取消", force=True)
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
        }
        if self.cost:
            extra_context = self.cost.compress_context(
                extra_context, self.config.context_findings_tokens,
                kind="context_compression", task_uid=uid, run_id=task.run_id,
            )
        task_pkg = self.prompts.write_task_package(
            task, agent, plan, self.memory.injectable(scope=task.task_id),
            runtime_dir=workdir, extra_context=extra_context, bootstrap=bootstrap,
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
                "agent_id": agent.agent_id, "model": self._model_for_agent(agent),
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
            model=self._model_for_agent(agent), timeout_seconds=task.spec.time_limit_minutes * 60,
            purpose="task",
        )
        adapter = create_adapter(agent)
        outcome = adapter.execute(request)
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
            self.store.set_task_status(uid, TaskStatus.WAITING_USER.value,
                                       error=outcome.text or "等待 Agent 执行", force=True)
            self.audit.log("control-plane", "task.waiting_user", subject=uid,
                           payload={"extras": outcome.extras})
            self.checkpoints.create("task_waiting_user", run_id=task.run_id, task_uid=uid)
            self.memory.sync_task_state(task.run_id)
            self.memory.sync_workflow(task.run_id)
            return
        if outcome.status != "completed":
            self._retry_or_fail(task, f"{outcome.status}: {outcome.error}")
            return
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
            if Path(target).exists():
                # A worktree left by an interrupted run. Reuse only if git agrees.
                proc = run_git(["rev-parse", "--is-inside-work-tree"], cwd=target, check=False)
                if proc.returncode == 0:
                    return str(target)
                shutil.rmtree(target, ignore_errors=True)
            create_worktree(self.project.root, target, run["base_commit"])
        ensure_dir(target)
        return str(target)

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

    def _model_for_agent(self, agent: AgentRecord) -> str:
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
    # Result -> patch -> verification
    # ------------------------------------------------------------------
    def _retry_or_fail(self, task: Task, error: str) -> None:
        fresh = self.store.get_task(task.uid) or task
        if fresh.attempts <= task.spec.retry_limit:
            self.store.set_task_status(task.uid, TaskStatus.FAILED.value, error=error, force=True)
            self.store.set_task_status(task.uid, TaskStatus.QUEUED.value, force=True)
            self.audit.log("control-plane", "task.retry", subject=task.uid,
                           payload={"attempt": fresh.attempts, "error": error[:500]})
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

    def _finish_verification(self, task: Task, agent: AgentRecord, result: ResultPackage) -> None:
        outcome: VerificationOutcome = self.verifier.verify(task, result)
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
        tasks = self.store.list_tasks(run_id)
        requeued = 0
        for task in tasks:
            if task.status not in (TaskStatus.WAITING_USER.value, TaskStatus.BLOCKED.value,
                                   TaskStatus.PAUSED.value, TaskStatus.FAILED.value):
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
                if waiting_for_approval and latest and latest.status == "approved":
                    self.store.set_task_status(task.uid, TaskStatus.QUEUED.value, force=True)
                    self.store.update_task(task.uid, error="")
                    requeued += 1
                    self.audit.log("control-plane", "task.requeued_after_approval",
                                   subject=task.uid,
                                   payload={"approval": latest.approval_id, "kind": latest.kind})
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
        if failed:
            self.store.update_run(run_id, status="failed", finished_at=now_iso(),
                                  error="存在失败任务")
            self.audit.log("control-plane", "run.failed", subject=run_id,
                           payload={"failed": [t.task_id for t in failed]})
            self.checkpoints.create("run_failed", run_id=run_id)
            self.memory.sync_workflow(run_id)
            return {"status": "failed", "run_id": run_id,
                    "failed": [t.task_id for t in failed]}
        if any(t.status != TaskStatus.COMPLETED.value for t in tasks):
            self.store.update_run(run_id, status="paused")
            self.memory.sync_workflow(run_id)
            return {"status": "paused", "run_id": run_id}

        summary = self._request_summary(run_id, plan)
        return summary

    def _request_summary(self, run_id: str, plan: TaskPlan) -> dict[str, Any]:
        run = self.store.get_run(run_id) or {}
        tasks = self.store.list_tasks(run_id)
        changes = [c.to_dict() for c in self.store.list_changes(run_id=run_id)]
        verifications = {t.uid: (self.store.latest_verification(t.uid) or {}) for t in tasks}
        ctx = self.context.summary_context(
            run=run, plan=plan.to_dict(), tasks=tasks,
            verification_by_task=verifications, changes=changes,
        )
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
        prompt_path, summary_path = self.prompts.write_summary_request(
            run_id=run_id, main_agent=main_agent, model=run.get("model", ""), context=ctx,
        )
        if self.bus:
            self.bus.publish("prompt", {
                "purpose": "summary", "run_id": run_id, "agent_id": main_agent.agent_id,
                "prompt_path": str(prompt_path), "context_tokens": self._estimate(ctx),
                "preview": read_text(prompt_path, default="")[:1200],
            }, source=main_agent.agent_id, correlation_id=run_id, sync_level=5)
        synthetic = Task(
            uid=f"{run_id}:SUMMARY", project_id=self.project.project_id,
            plan_id=plan.plan_id, run_id=run_id, spec=plan.tasks[0] if plan.tasks else TaskSpec(
                task_id="SUMMARY", title="汇总", objective="汇总", type="analysis"
            ),
            status=TaskStatus.RUNNING.value, workdir=summary_workspace,
            task_dir=str(prompt_path.parent), base_commit=run.get("base_commit", ""),
        )
        request = ExecutionRequest(
            project_id=self.project.project_id, project_root=self.project.root,
            task=synthetic, workdir=summary_workspace, task_dir=str(prompt_path.parent),
            prompt_path=str(prompt_path), result_path=str(summary_path),
            model=run.get("model", ""), timeout_seconds=self.config.summary_timeout_seconds,
            purpose="summary",
        )
        outcome = create_adapter(main_agent).execute(request)
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
        data = outcome.result_payload if outcome.result_payload is not None else read_json(summary_path)
        if data is None:
            self.store.update_run(run_id, status="failed", finished_at=now_iso(),
                                  error="主节点未输出汇总")
            return {"status": "failed", "run_id": run_id, "error": "主节点未输出汇总"}
        return self.finalize_summary(run_id, data)

    def submit_summary(self, run_id: str, payload: dict[str, Any] | str | Path) -> dict[str, Any]:
        data = read_json(payload) if isinstance(payload, (str, Path)) else payload
        if not isinstance(data, dict):
            raise ValidationError("汇总必须是 JSON 对象")
        return self.finalize_summary(run_id, data)

    def finalize_summary(self, run_id: str, data: dict[str, Any]) -> dict[str, Any]:
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
        run = self.store.get_run(run_id) or {}
        tasks = self.store.list_tasks(run_id)
        patches = [
            {"task_id": t.task_id, "agent": t.assigned_agent, "patch": t.patch_path,
             "verification": t.verification_status}
            for t in tasks if t.patch_path
        ]
        plan = self.store.get_plan(run["plan_id"])
        if self.governance and plan:
            self.store.update_run(run_id, status="pending_acceptance", summary=data,
                                  finished_at=now_iso())
            approval = self.governance.ensure_acceptance_approval(run_id, plan, data, tasks)
            status = "pending_acceptance"
            checkpoint_kind = "before_acceptance"
        else:
            self.store.update_run(run_id, status="pending_merge", summary=data,
                                  finished_at=now_iso())
            approval = self.approvals.ensure_merge_approval(
                run_id, plan.goal if plan else "", patches, data,
            )
            status = "pending_merge"
            checkpoint_kind = "before_merge"
        self.checkpoints.create(checkpoint_kind, run_id=run_id,
                                extra={"approval": approval.approval_id,
                                       "patches": len(patches)})
        self.audit.log("control-plane", "run.summary", subject=run_id,
                       payload={"recommendation": data["merge_recommendation"],
                                "approval": approval.approval_id, "status": status})
        self.memory.record_decision(
            f"主节点汇总：{data.get('completion', '')[:300]}",
            agent_id=run.get("main_agent_id", ""),
            model=run.get("model", ""),
            approved=False,
        )
        self.memory.sync_workflow(run_id)
        return {"status": status, "run_id": run_id,
                "approval_id": approval.approval_id, "summary": data}

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
        self.store.set_task_status(uid, TaskStatus.QUEUED.value, force=True)
        self.audit.log(by, "task.retry_requested", subject=uid)
        return {"status": "queued", "task_uid": uid}
