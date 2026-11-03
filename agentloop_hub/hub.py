"""Hub facade: one object wiring the control plane, adapters and persistence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .adapters import DiscoveryContext, create_adapter, discover_agents
from .config import HubConfig, load_config
from .connectors import CONNECTOR_PROFILES, connector_manifest, is_auto_executable
from .errors import ApprovalRequiredError, ConfigError, NotFoundError, ValidationError
from .models import AgentRecord, Project, Task, TaskSpec, TaskType
from .sessions.service import SessionImportService
from .storage.db import Database
from .storage.repo import Store
from .util import atomic_write_text, sha1_hex
from .workspace import detect_repo, is_git_repo
from .workspace.identity import build_project, canonical_path
from .control.approvals import ApprovalService
from .control.audit import AuditService
from .control.budget import BudgetManager
from .control.bus import ContextBus
from .control.checkpoints import CheckpointService
from .control.context import ContextBuilder
from .control.cost import CostOptimizer
from .control.governance import GovernanceService
from .control.handoff import HandoffService
from .control.memory import MemoryService
from .control.merge import MergeService
from .control.model_catalog import ModelCatalogService
from .control.paths import WorkspacePaths
from .control.planning import PlanningService
from .control.recovery import RecoveryService
from .control.router import CapabilityRouter
from .control.runner import RunService
from .control.sync import SyncService
from .control.verification import Verifier
from .control.versioning import AgentVersionService


class Hub:
    def __init__(self, project_root: str | Path, *, config: HubConfig | None = None,
                 auto_discover: bool = True):
        self.root = Path(project_root).expanduser().resolve()
        if not self.root.exists() or not self.root.is_dir():
            raise ConfigError(f"项目目录不存在: {self.root}")
        self.paths = WorkspacePaths(self.root)
        self.paths.ensure()
        self.config = config or load_config(self.root)
        self.db = Database(self.paths.db)
        self.store = Store(self.db)
        self._new_project = False
        self.project = self._open_project()
        # The bus is the single information path; Store writes automatically
        # fan out task/workflow/knowledge/file/governance/message/response events.
        self.bus = ContextBus(self.store, self.project, self.config)
        self.store.event_sink = self.bus
        self.audit = AuditService(self.store, self.project.project_id)
        self.budget = BudgetManager(self.store, self.project, self.config)
        self.approvals = ApprovalService(self.store, self.project, self.config)
        self.memory = MemoryService(self.store, self.project, self.config)
        self.context = ContextBuilder(self.store, self.project, self.config)
        self.router = CapabilityRouter(self.store, self.project, self.config)
        self.cost = CostOptimizer(self.store, self.project, self.config)
        self.governance = GovernanceService(
            self.store, self.project, self.approvals, self.audit,
        )
        self.checkpoints = CheckpointService(self.store, self.project, self.paths, self.budget)
        self.verifier = Verifier(self.project, self.paths, self.config)
        self.planning = PlanningService(
            self.store, self.project, self.paths, self.config, self.approvals,
            self.audit, self.budget, self.memory, self.context, self.checkpoints,
            bus=self.bus, cost=self.cost, governance=self.governance,
        )
        self.runner = RunService(
            self.store, self.project, self.paths, self.config, self.approvals,
            self.audit, self.budget, self.memory, self.context, self.checkpoints, self.verifier,
            bus=self.bus, router=self.router, cost=self.cost, governance=self.governance,
        )
        self.merge_service = MergeService(
            self.store, self.project, self.paths, self.config, self.approvals,
            self.audit, self.checkpoints, memory=self.memory,
        )
        self.handoff_service = HandoffService(
            self.store, self.project, self.paths, self.approvals, self.memory,
        )
        self.recovery = RecoveryService(
            self.store, self.project, self.paths, self.audit, self.budget, self.checkpoints,
        )
        self.sessions = SessionImportService(self.store, self.project)
        self.sync_service = SyncService(
            self.store, self.project, self.memory, self.sessions, self.bus,
        )
        self.model_catalog = ModelCatalogService(
            self.store, self.project, self.config,
        )
        self.versions = AgentVersionService(self.store, self.project)
        if self._new_project:
            self.memory.seed_project_facts()
            self.memory.seed_project_index()
            self.memory.seed_code_structure()
            self.audit.log("user", "project.created", subject=self.project.project_id,
                           payload={"root": self.project.root, "branch": self.project.branch})
        else:
            # Upgrade path: existing v1 workspaces get the v2 project/code index
            # the first time the new controller opens them.
            if not self.memory.list(type="project_index", limit=1):
                self.memory.seed_project_index()
            if not self.memory.list(type="code_structure", limit=1):
                self.memory.seed_code_structure()
        if auto_discover:
            self.discover(merge=True)

    # ------------------------------------------------------------------
    # Project lifecycle
    # ------------------------------------------------------------------
    def _open_project(self) -> Project:
        existing = self.store.get_project(self._project_id())
        if existing:
            info = detect_repo(self.root)
            if info.get("is_git"):
                self.store.update_project_git(
                    existing.project_id, branch=info.get("branch", ""),
                    commit=info.get("commit", ""), git_remote=info.get("remote", ""),
                )
                existing = self.store.get_project(existing.project_id) or existing
            self._write_project_yaml(existing)
            from .workspace.gitops import add_info_exclude
            add_info_exclude(self.root, [".agentloop/", ".agentloop-task/",
                                         ".agentmesh/", ".workbuddy/",
                                         "__pycache__/", ".pytest_cache/"])
            return existing
        info = detect_repo(self.root)
        if not info.get("is_git"):
            raise ConfigError(
                f"MVP 仅支持本地 Git 项目（{self.root} 不是 Git 仓库）。请先执行 git init 或选择 Git 项目。"
            )
        project = build_project(self.root, project_id=self._project_id(), git_info=info)
        self.store.save_project(project)
        self._write_project_yaml(project)
        from .workspace.gitops import add_info_exclude
        add_info_exclude(self.root, [".agentloop/", ".agentloop-task/",
                                     ".agentmesh/", ".workbuddy/",
                                     "__pycache__/", ".pytest_cache/"])
        self._new_project = True
        return project

    def _project_id(self) -> str:
        return "prj_" + sha1_hex(canonical_path(self.root))[:12]

    def _write_project_yaml(self, project: Project) -> None:
        data = project.to_dict()
        atomic_write_text(self.paths.project_yaml,
                          yaml.safe_dump(data, allow_unicode=True, sort_keys=False))

    def refresh_git(self) -> dict[str, Any]:
        info = detect_repo(self.root)
        if info.get("is_git"):
            self.store.update_project_git(self.project.project_id, branch=info["branch"],
                                          commit=info["commit"], git_remote=info["remote"])
            self.project = self.store.get_project(self.project.project_id) or self.project
            self.budget.project = self.project
        return info

    def set_project_budget(self, tokens: int) -> dict[str, Any]:
        if tokens < 0:
            raise ValidationError("预算不能为负")
        extra = dict(self.project.extra or {})
        extra["token_budget"] = int(tokens)
        self.project.extra = extra
        self.store.save_project(self.project)
        self.audit.log("user", "project.budget_set", payload={"tokens": tokens})
        return {"token_budget": tokens, "used": self.budget.used_project()}

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------
    def discover(self, *, merge: bool = True, ctx: DiscoveryContext | None = None) -> list[AgentRecord]:
        discovered = discover_agents(self.project.project_id, ctx)
        out = []
        for record in discovered:
            existing = self.store.get_agent(record.agent_id)
            if existing and not existing.discovered and merge:
                out.append(existing)
                continue
            if existing:
                record.created_at = existing.created_at
                # Preserve user permissions, explicit project roots and the
                # user-selected model across Agent software upgrades.
                record.permissions = existing.permissions or record.permissions
                record.extra = {**(existing.extra or {}), **(record.extra or {})}
            if record.models:
                self.store.replace_discovered_models(
                    self.project.project_id, record.agent_id, record.models,
                    source="discovered",
                )
            catalog_models = sorted(
                {row["model"] for row in self.store.list_agent_models(
                    self.project.project_id, agent_id=record.agent_id)}
                | set(record.models or [])
            )
            if catalog_models:
                record.models = catalog_models
            elif existing:
                record.models = list(existing.models or [])
            self.store.upsert_agent(record)
            self.versions.record([record])
            out.append(record)
        self.audit.log("control-plane", "agents.discovered",
                       payload={"agents": [a.agent_id for a in out],
                                "online": [a.agent_id for a in out if a.status == "online"],
                                "versions": [{"agent_id": a.agent_id, "version": a.version}
                                             for a in out]})
        return out

    def register_agent(self, data: dict[str, Any]) -> AgentRecord:
        record = AgentRecord(
            agent_id=str(data.get("agent_id") or ""),
            name=str(data.get("name") or data.get("agent_id") or ""),
            product=str(data.get("product") or data.get("name") or ""),
            edition=str(data.get("edition") or ""),
            project_id=self.project.project_id,
            version=str(data.get("version") or "unknown"),
            adapter_type=str(data.get("adapter_type") or "file_relay"),
            adapter_config=dict(data.get("adapter_config") or {}),
            access_level=str(data.get("access_level") or "L2"),
            capabilities=dict(data.get("capabilities") or {
                "read_files": True, "write_files": True, "terminal": True,
                "web_search": False, "mcp": False, "session_read": False,
                "session_write": False, "structured_output": False,
            }),
            models=list(data.get("models") or []),
            permissions=dict(data.get("permissions") or {
                "allow": ["read_files", "write_files", "terminal"],
                "allowed_roots": [self.project.root],
                "allow_network": False,
                "allow_shell": True,
            }),
            status=str(data.get("status") or "online"),
            discovered=False,
        )
        existing = self.store.get_agent(record.agent_id)
        if existing:
            record.created_at = existing.created_at
            record.extra = {**(existing.extra or {}), **(record.extra or {})}
            record.permissions = record.permissions or existing.permissions
        self.store.upsert_agent(record)
        for model in list(record.models):
            self.store.upsert_agent_model(
                self.project.project_id, record.agent_id, model,
                source="registered", note="Agent 注册时登记",
            )
        self.versions.record([record])
        self.audit.log("user", "agent.registered", subject=record.agent_id,
                       payload={"adapter_type": record.adapter_type,
                                "access_level": record.access_level,
                                "models": list(record.models)})
        return record

    def list_agents(self) -> list[AgentRecord]:
        return self.store.list_agents()

    def get_agent(self, agent_id: str) -> AgentRecord:
        agent = self.store.get_agent(agent_id)
        if not agent:
            raise NotFoundError(f"Agent 不存在: {agent_id}")
        return agent

    def remove_agent(self, agent_id: str) -> None:
        self.get_agent(agent_id)
        self.store.delete_agent(agent_id)
        self.audit.log("user", "agent.removed", subject=agent_id)

    def agent_health(self, agent_id: str) -> dict[str, Any]:
        agent = self.get_agent(agent_id)
        try:
            health = create_adapter(agent).health()
        except Exception as exc:
            health = {"agent_id": agent_id, "available": False, "status": "error", "error": str(exc)}
        self.store.set_agent_status(agent_id, health.get("status", "unknown"), health.get("error", ""))
        health["metrics"] = self.agent_metrics(agent_id)
        return health

    def agent_metrics(self, agent_id: str) -> dict[str, Any]:
        tasks = [t for t in self.store.list_tasks_by_project(self.project.project_id, limit=1000)
                 if t.assigned_agent == agent_id]
        completed = [t for t in tasks if t.status == "completed"]
        failed = [t for t in tasks if t.status == "failed"]
        success_rate = (len(completed) / len(tasks)) if tasks else 0.0
        agent = self.store.get_agent(agent_id)
        return {
            "agent_id": agent_id,
            "tasks_total": len(tasks),
            "tasks_completed": len(completed),
            "tasks_failed": len(failed),
            "success_rate": round(success_rate, 4),
            "last_seen": agent.last_seen if agent else "",
            "last_error": agent.last_error if agent else "",
            "status": agent.status if agent else "unknown",
        }

    def set_main(self, agent_id: str, model: str = "", *, budget_tokens: int = 0,
                 permissions: dict[str, Any] | None = None, note: str = "") -> dict[str, Any]:
        self.get_agent(agent_id)
        self.store.set_main_node(self.project.project_id, agent_id, model,
                                 budget_tokens=budget_tokens, permissions=permissions, note=note)
        self.audit.log("user", "main_node.set", subject=agent_id,
                       payload={"model": model, "budget_tokens": budget_tokens})
        return self.get_main()

    def auto_main(self, *, agent_id: str = "", model: str = "") -> dict[str, Any]:
        """Pick a main Agent using router ranking plus the auto-executable gate."""
        selection: dict[str, Any]
        if agent_id:
            agent = self.get_agent(agent_id)
            if not is_auto_executable(agent):
                raise ValidationError(
                    f"Agent {agent_id} 不可自动执行（{agent.adapter_type} 需要人工接力），"
                    "不能作为自动主节点",
                    details={"agent_id": agent_id, "adapter_type": agent.adapter_type},
                )
            selection = {
                "selected": {
                    "agent_id": agent_id, "score": None,
                    "reason": "用户指定且通过自动执行门槛",
                },
                "candidates": [],
                "mode": "explicit",
            }
        else:
            preview = self.route_preview(task_type="architecture")
            selection = {**preview, "mode": "router"}
            selected = preview.get("selected")
            if not selected:
                raise ValidationError(
                    "没有满足架构/规划能力且可自动执行的主节点候选",
                    details={"candidates": preview.get("candidates", [])},
                )
            agent_id = str(selected["agent_id"])
        agent = self.get_agent(agent_id)
        chosen_model = model or str((agent.extra or {}).get("selected_model") or "")
        if not chosen_model and agent.models:
            chosen_model = agent.models[0]
        main = self.set_main(agent_id, chosen_model, note="auto selected")
        reason = str((selection.get("selected") or {}).get("reason") or "")
        self.audit.log("control-plane", "main.auto_selected", subject=agent_id,
                       payload={"model": chosen_model, "reason": reason,
                                "mode": selection.get("mode")})
        self.bus.publish("governance", {
            "action": "main_auto_selected", "agent_id": agent_id,
            "model": chosen_model, "reason": reason,
            "candidates": selection.get("candidates", []),
        }, source="control-plane", correlation_id=self.project.project_id, sync_level=0)
        return {"main": main, "selection": selection,
                "agent_id": agent_id, "model": chosen_model}

    def get_main(self) -> dict[str, Any] | None:
        row = self.store.get_main_node(self.project.project_id)
        if not row:
            return None
        agent = self.store.get_agent(row["agent_id"])
        return {
            **row,
            "agent": agent.to_dict() if agent else None,
        }

    def main_history(self) -> list[dict[str, Any]]:
        return self.store.main_node_history(self.project.project_id)

    # ------------------------------------------------------------------
    # Models / versions / MCP
    # ------------------------------------------------------------------
    def agent_models(self, *, refresh: bool = False) -> dict[str, Any]:
        return self.model_catalog.catalog(refresh=refresh)

    def refresh_agent_models(self, agent_id: str = "") -> dict[str, Any]:
        result = self.model_catalog.refresh(agent_id=agent_id)
        self.audit.log("user", "models.refreshed",
                       subject=agent_id,
                       payload={"agents": [a["agent_id"] for a in result["agents"]]})
        return result

    def set_agent_model(self, agent_id: str, model: str) -> dict[str, Any]:
        result = self.model_catalog.select(agent_id, model)
        self.audit.log("user", "agent.model_selected", subject=agent_id,
                       payload={"model": result.get("selected_model", "")})
        return result

    def set_agent_model_programmatically(self, agent_id: str, model: str) -> dict[str, Any]:
        from .control.interfaces import set_model_programmatically
        from .gui import profile_for_app
        agent = self.get_agent(agent_id)
        profile = profile_for_app(agent.agent_id)
        app = profile.app if profile else agent.agent_id
        outcome = set_model_programmatically(app, model, self.config.to_dict())
        self.audit.log("user", "agent.model_interface", subject=agent_id,
                       payload={"app": app, "model": model, "outcome": outcome.to_dict()})
        if outcome.ok:
            self.model_catalog.select(agent_id, model)
        return {"agent_id": agent_id, "app": app, **outcome.to_dict()}

    def register_agent_models(self, agent_id: str, models: list[str],
                              note: str = "") -> dict[str, Any]:
        result = self.model_catalog.register(agent_id, models, note=note)
        self.audit.log("user", "agent.models_registered", subject=agent_id,
                       payload={"models": models})
        return result

    def remove_agent_model(self, agent_id: str, model: str) -> dict[str, Any]:
        result = self.model_catalog.remove(agent_id, model)
        self.audit.log("user", "agent.model_removed", subject=agent_id,
                       payload={"model": model})
        return result

    def agent_versions(self, agent_id: str = "", *, limit: int = 200) -> dict[str, Any]:
        return self.versions.history(agent_id, limit=limit)

    def agent_version_status(self) -> dict[str, Any]:
        return self.versions.status()

    def mcp_tools(self, agent_id: str) -> dict[str, Any]:
        agent = self.get_agent(agent_id)
        adapter = create_adapter(agent)
        if not hasattr(adapter, "tools"):
            raise ValidationError(f"Agent {agent_id} 不是 MCP 适配器")
        result = adapter.tools()  # type: ignore[attr-defined]
        self.audit.log("user", "mcp.tools_listed", subject=agent_id,
                       payload={"count": result.get("count", 0)})
        return result

    def mcp_call(self, agent_id: str, tool: str,
                 arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        agent = self.get_agent(agent_id)
        adapter = create_adapter(agent)
        if not hasattr(adapter, "call"):
            raise ValidationError(f"Agent {agent_id} 不是 MCP 适配器")
        result = adapter.call(tool, arguments or {})  # type: ignore[attr-defined]
        self.audit.log("user", "mcp.tool_called", subject=agent_id,
                       payload={"tool": tool, "arguments": arguments or {}})
        return result

    # ------------------------------------------------------------------
    # Planning / runs
    # ------------------------------------------------------------------
    def request_plan(self, goal: str, *, main_agent_id: str = "", model: str = "") -> dict[str, Any]:
        return self.planning.request_plan(goal, main_agent_id=main_agent_id, model=model)

    def import_plan(self, data: dict[str, Any] | str | Path, *, main_agent_id: str = "",
                    model: str = "") -> dict[str, Any]:
        return self.planning.import_plan(data, main_agent_id=main_agent_id, model=model)

    def approve_plan(self, plan_id: str, *, approval_id: str = "", note: str = "",
                     by: str = "user") -> dict[str, Any]:
        return self.planning.approve_plan(plan_id, approval_id=approval_id, note=note, by=by)

    def reject_plan(self, plan_id: str, *, approval_id: str = "", note: str = "") -> dict[str, Any]:
        return self.planning.reject_plan(plan_id, approval_id=approval_id, note=note)

    def plans(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.planning.list_plans(limit=limit)

    def plan(self, plan_id: str) -> Any:
        plan = self.store.get_plan(plan_id)
        if not plan:
            raise NotFoundError(f"计划不存在: {plan_id}")
        return plan.to_dict()

    def start_run(self, run_id: str, *, max_parallel: int | None = None,
                  background: bool = False) -> dict[str, Any]:
        return self.runner.start(run_id, max_parallel=max_parallel, background=background)

    def resume_run(self, run_id: str, *, max_parallel: int | None = None,
                   background: bool = False) -> dict[str, Any]:
        return self.runner.resume(run_id, max_parallel=max_parallel, background=background)

    def run_status(self, run_id: str) -> dict[str, Any]:
        return self.runner.status(run_id)

    def runs(self, *, limit: int = 30) -> list[dict[str, Any]]:
        return self.store.list_runs(self.project.project_id, limit=limit)

    def latest_run(self) -> dict[str, Any] | None:
        return self.store.latest_run(self.project.project_id)

    def tasks(self, *, run_id: str = "", limit: int = 200) -> list[dict[str, Any]]:
        if run_id:
            return [t.to_dict() for t in self.store.list_tasks(run_id)]
        return [t.to_dict() for t in self.store.list_tasks_by_project(self.project.project_id, limit)]

    def task(self, uid: str) -> Task:
        task = self.store.get_task(uid)
        if not task:
            raise NotFoundError(f"任务不存在: {uid}")
        return task

    def task_detail(self, uid: str, *, level: str = "summary") -> dict[str, Any]:
        task = self.task(uid)
        detail = self.context.task_detail(task, level=level)
        self.audit.log("user", "context.read", subject=uid, payload={"level": level})
        return detail

    def submit_result(self, uid: str, payload: dict[str, Any] | str | Path) -> dict[str, Any]:
        return self.runner.submit_result(uid, payload)

    def submit_summary(self, run_id: str, payload: dict[str, Any] | str | Path) -> dict[str, Any]:
        return self.runner.submit_summary(run_id, payload)

    def cancel_task(self, uid: str) -> dict[str, Any]:
        return self.runner.cancel_task(uid)

    def retry_task(self, uid: str) -> dict[str, Any]:
        return self.runner.retry_task(uid)

    def merge(self, run_id: str, *, approval_id: str = "", commit: bool = False) -> dict[str, Any]:
        return self.merge_service.merge(run_id, approval_id=approval_id, commit=commit)

    def rollback_task(self, uid: str) -> dict[str, Any]:
        return self.merge_service.rollback_task(uid)

    def rollback_file(self, uid: str, path: str) -> dict[str, Any]:
        return self.merge_service.rollback_file(uid, path)

    def rollback_run(self, run_id: str) -> dict[str, Any]:
        return self.merge_service.rollback_run(run_id)

    # ------------------------------------------------------------------
    # Approvals / costs / audit / memory / sessions
    # ------------------------------------------------------------------
    def approvals_pending(self) -> list[dict[str, Any]]:
        return [a.to_dict() for a in self.approvals.pending()]

    def approvals_all(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return [a.to_dict() for a in self.approvals.all(limit=limit)]

    def decide_approval(self, approval_id: str, *, approve: bool, note: str = "") -> dict[str, Any]:
        existing = self.store.get_approval(approval_id)
        if not existing:
            raise NotFoundError(f"审批不存在: {approval_id}")
        if approve and existing.kind == "plan" and existing.plan_id \
                and not self.governance.architecture_approved(existing.plan_id):
            raise ApprovalRequiredError(
                "计划包含架构设计，需先通过架构审批",
                details={"plan_id": existing.plan_id, "approval_id": approval_id},
            )
        approval = self.approvals.decide(approval_id, approved=approve, note=note)
        if approval.kind == "plan" and approval.plan_id:
            if approve:
                result = self.planning.approve_plan(approval.plan_id, approval_id=approval_id, note=note)
            else:
                self.planning.reject_plan(approval.plan_id, approval_id=approval_id, note=note)
                result = {"status": "rejected", "plan_id": approval.plan_id}
            return {"approval": approval.to_dict(), "outcome": result}
        if approval.kind == "architecture" and approval.plan_id:
            return {"approval": approval.to_dict(),
                    "outcome": {"status": "approved" if approve else "rejected",
                                "plan_id": approval.plan_id,
                                "next": "批准计划（plan approve）" if approve else "修改计划后重新导入"}}
        if approval.kind == "acceptance" and approval.run_id:
            if approve:
                outcome = self.governance.accept_run(
                    approval.run_id, approval_id=approval_id, by="user",
                )
            else:
                outcome = self.governance.reject_run(
                    approval.run_id, approval_id=approval_id, by="user",
                )
            return {"approval": approval.to_dict(), "outcome": outcome}
        if approval.kind == "merge" and approve and approval.run_id:
            return {"approval": approval.to_dict(),
                    "outcome": self.merge(approval.run_id, approval_id=approval_id)}
        if approval.kind == "release" and approval.run_id:
            if approve:
                outcome = self.governance.release_run(
                    approval.run_id, approval_id=approval_id, by="user",
                )
            else:
                self.store.update_run(approval.run_id, release_status="rejected")
                outcome = {"status": "release_rejected", "run_id": approval.run_id}
            return {"approval": approval.to_dict(), "outcome": outcome}
        return {"approval": approval.to_dict()}

    def costs(self, *, run_id: str = "") -> dict[str, Any]:
        snapshot = self.budget.snapshot(run_id=run_id)
        snapshot["optimizer"] = self.cost.report(run_id=run_id)
        return snapshot

    def usage(self, *, limit: int = 500) -> list[dict[str, Any]]:
        return [u.to_dict() for u in self.store.list_usage(self.project.project_id, limit=limit)]

    def audit_events(self, *, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        return self.audit.list(limit=limit, offset=offset)

    def audit_verify(self) -> dict[str, Any]:
        return self.audit.verify()

    def memory_list(self, *, type: str = "", status: str = "", limit: int = 500) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self.memory.list(type=type, status=status, limit=limit)]

    def memory_add(self, **kwargs: Any) -> dict[str, Any]:
        return self.memory.add(**kwargs).to_dict()

    def memory_search(self, keyword: str) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self.memory.search(keyword)]

    def memory_update(self, memory_id: str, **fields: Any) -> dict[str, Any]:
        return self.memory.update(memory_id, **fields).to_dict()

    def memory_delete(self, memory_id: str) -> dict[str, Any]:
        self.memory.delete(memory_id)
        return {"status": "deleted", "memory_id": memory_id}

    def memory_resolve(self, memory_id: str, *, keep: bool) -> dict[str, Any]:
        self.memory.resolve_conflict(memory_id, keep=keep)
        return {"status": "resolved", "memory_id": memory_id, "keep": keep}

    def import_sessions(self, agent_id: str, *, limit: int = 20,
                        include_unscoped: bool = False) -> dict[str, Any]:
        agent = self.get_agent(agent_id)
        result = self.sessions.import_for_agent(agent, limit=limit, include_unscoped=include_unscoped)
        for record in result.records:
            self.memory.sync_conversation(record)
        return result.to_dict()

    def conversations(self, *, agent_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        return self.store.list_conversations(self.project.project_id, agent_id=agent_id, limit=limit)

    def handoff(self, *, to_agent_id: str = "", model: str = "", note: str = "") -> dict[str, Any]:
        if to_agent_id:
            agent = self.get_agent(to_agent_id)
            return self.handoff_service.switch(to_agent=agent, model=model or "", note=note)
        package = self.handoff_service.build_package(note=note)
        files = self.handoff_service.write(package, prefix="manual")
        self.audit.log("user", "handoff.generated", payload={"files": files})
        return {"package": package, "files": files}

    def recover(self, *, run_id: str = "", auto_resume: bool = False) -> dict[str, Any]:
        return self.recovery.recover(run_id=run_id, auto_resume=auto_resume)

    def recovery_report(self) -> dict[str, Any]:
        return self.recovery.inspect()

    # ------------------------------------------------------------------
    # AgentMesh OS: Connector / Bus / Router / Cost / Governance / Sync
    # ------------------------------------------------------------------
    def connector_manifest(self) -> dict[str, Any]:
        return connector_manifest(self.list_agents())

    def agent_ready(self, agent_id: str) -> dict[str, Any]:
        agent = self.get_agent(agent_id)
        return self.sync_service.agent_ready(agent)

    def sync_status(self) -> dict[str, Any]:
        return self.sync_service.status()

    def run_sync(self, *, agent_id: str = "", limit: int = 20,
                 include_unscoped: bool = False) -> dict[str, Any]:
        return self.sync_service.run(agent_id=agent_id, limit=limit,
                                     include_unscoped=include_unscoped)

    def bootstrap(self, *, agent_id: str = "", scope: str = "") -> dict[str, Any]:
        return self.sync_service.bootstrap(agent_id=agent_id, scope=scope)

    def bus_events(self, *, after_seq: int = 0, limit: int = 100, topic: str = "",
                   kind: str = "", correlation_id: str = "") -> dict[str, Any]:
        events = self.bus.events(after_seq=after_seq, limit=limit, topic=topic,
                                 kind=kind, correlation_id=correlation_id)
        return {"events": events, "count": len(events),
                "last_seq": events[-1]["seq"] if events else after_seq}

    def bus_publish(self, kind: str, payload: dict[str, Any], *, source: str = "user",
                    correlation_id: str = "", topic: str = "") -> dict[str, Any]:
        return self.bus.publish(kind, payload, source=source,
                                correlation_id=correlation_id, topic=topic)

    def bus_subscribe(self, subscriber: str, *, topics: list[str] | None = None,
                      kinds: list[str] | None = None) -> dict[str, Any]:
        return self.bus.subscribe(subscriber, topics=topics, kinds=kinds)

    def bus_poll(self, subscription_id: str, *, limit: int = 100) -> dict[str, Any]:
        return self.bus.poll(subscription_id, limit=limit)

    def bus_unsubscribe(self, subscription_id: str) -> dict[str, Any]:
        return self.bus.unsubscribe(subscription_id)

    def bus_stats(self) -> dict[str, Any]:
        return self.bus.stats()

    def bus_verify(self) -> dict[str, Any]:
        return self.bus.verify()

    def bus_subscriptions(self) -> list[dict[str, Any]]:
        return self.bus.subscriptions()

    def route_task(self, uid: str) -> dict[str, Any]:
        task = self.task(uid)
        return self.router.rank_task(task)

    def route_preview(self, *, task_type: str = "analysis",
                      required: list[str] | None = None) -> dict[str, Any]:
        if task_type not in {t.value for t in TaskType}:
            raise ValidationError(f"未知任务类型: {task_type}")
        spec = TaskSpec(
            task_id="preview", title="路由预览", objective="路由预览",
            type=task_type, required_capabilities=list(required or []),
            token_budget=0, time_limit_minutes=1, retry_limit=0,
        )
        return self.router.rank_spec(spec)

    def router_scores(self) -> dict[str, Any]:
        return self.router.scores()

    def routing_decisions(self, *, run_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        return self.store.list_routing(self.project.project_id, run_id=run_id, limit=limit)

    def benchmark(self) -> dict[str, Any]:
        return self.cost.benchmark(persist=True)

    def cost_report(self, *, run_id: str = "") -> dict[str, Any]:
        return self.cost.report(run_id=run_id)

    def clear_semantic_cache(self) -> dict[str, Any]:
        before = self.store.semantic_stats(self.project.project_id)
        self.store.semantic_clear(self.project.project_id)
        self.audit.log("user", "cache.semantic_cleared", payload={"before": before})
        return {"status": "cleared", "before": before}

    def governance_status(self, run_id: str) -> dict[str, Any]:
        return self.governance.status(run_id)

    def request_release(self, run_id: str) -> dict[str, Any]:
        approval = self.governance.ensure_release_approval(run_id)
        return {"status": "approved" if approval.status == "approved" else "pending_approval",
                "run_id": run_id, "approval_id": approval.approval_id,
                "release_status": self.store.get_run(run_id).get("release_status", "")}

    def ask(self, goal: str, *, main_agent_id: str = "", model: str = "") -> dict[str, Any]:
        """Single entry point: one goal in, structured plan + approvals out."""
        return self.planning.request_plan(goal, main_agent_id=main_agent_id, model=model)

    # ------------------------------------------------------------------
    # Overview / doctor
    # ------------------------------------------------------------------
    def overview(self) -> dict[str, Any]:
        run = self.latest_run()
        tasks = self.store.list_tasks(run["run_id"]) if run else []
        counts: dict[str, int] = {}
        for task in tasks:
            counts[task.status] = counts.get(task.status, 0) + 1
        agents = self.list_agents()
        return {
            "project": self.project.to_dict(),
            "main": self.get_main(),
            "agents": {
                "total": len(agents),
                "online": len([a for a in agents if a.status == "online"]),
                "by_level": _count_by(agents, lambda a: a.access_level),
                "list": [{**a.to_dict(), "metrics": self.agent_metrics(a.agent_id)}
                         for a in agents],
            },
            "latest_run": run,
            "task_counts": counts,
            "tasks": [t.to_dict() for t in tasks],
            "pending_approvals": [a.to_dict() for a in self.approvals.pending()],
            "budget": self.budget.snapshot(run_id=run["run_id"] if run else ""),
            "checkpoint": self.checkpoints.latest(run_id=run["run_id"] if run else ""),
            "plans": self.plans(limit=10),
            "conversations": len(self.store.list_conversations(self.project.project_id, limit=1000)),
            "data_dir": str(self.paths.base),
            "config": self.config.to_dict(),
            "bus": self.bus.stats(),
            "connectors": {
                "protocol": "agentmesh.connector.v1",
                "count": len(agents),
                "supported_products": len(CONNECTOR_PROFILES),
                "manifest": self.connector_manifest(),
            },
            "cost_optimizer": {
                "savings": self.store.savings_totals(self.project.project_id),
                "semantic_cache": self.store.semantic_stats(self.project.project_id),
                "benchmarks": len(self.store.latest_benchmarks(self.project.project_id)),
            },
            "router": self.router.scores(),
            "governance": (
                self.governance.status(run["run_id"]) if run else
                {"status": "no_run", "acceptance_status": "", "release_status": ""}
            ),
            "sync": self.sync_status(),
            "models": self.model_catalog.catalog(),
            "agent_versions": self.versions.status(),
            "modules": {
                "connector": "ready", "unified_memory": "ready", "context_bus": "ready",
                "task_scheduler": "ready", "capability_router": "ready",
                "cost_optimizer": "ready", "human_governance": "ready",
            },
        }

    def doctor(self) -> dict[str, Any]:
        checks: dict[str, Any] = {}
        checks["git"] = {"ok": is_git_repo(self.root), "info": detect_repo(self.root)}
        checks["database"] = {"ok": self.paths.db.exists(), "path": str(self.paths.db)}
        checks["data_dir"] = {"ok": self.paths.base.exists(), "path": str(self.paths.base)}
        checks["agents"] = []
        for agent in self.list_agents():
            try:
                health = create_adapter(agent).health()
            except Exception as exc:
                health = {"agent_id": agent.agent_id, "available": False, "error": str(exc)}
            checks["agents"].append(health)
        pending = self.approvals.pending()
        checks["pending_approvals"] = {"ok": True, "count": len(pending)}
        checks["audit_chain"] = self.audit.verify()
        checks["main_node"] = {"ok": bool(self.get_main()), "value": self.get_main()}
        checks["context_bus"] = self.bus.verify()
        checks["connectors"] = {
            "ok": len(self.list_agents()) >= 1,
            "count": len(self.list_agents()),
            "manifest": "agentmesh.connector.v1",
        }
        checks["memory"] = {
            "ok": len(self.store.list_memories(self.project.project_id, status="active")) > 0,
            "active": len(self.store.list_memories(self.project.project_id, status="active")),
            "types": sorted({m.type for m in self.store.list_memories(self.project.project_id, limit=1000)}),
        }
        checks["governance"] = (
            self.governance.status(run["run_id"]) if (run := self.latest_run()) else {"status": "no_run"}
        )
        checks["cost_optimizer"] = {
            "semantic_cache": self.store.semantic_stats(self.project.project_id),
            "savings": self.store.savings_totals(self.project.project_id),
        }
        version_status = self.versions.status()
        checks["agent_versions"] = {
            "ok": version_status["drift_count"] == 0,
            "drift_count": version_status["drift_count"],
            "drifts": version_status["drifts"],
            "total_versions": version_status["total_versions"],
        }
        catalog = self.model_catalog.catalog()
        checks["models"] = {
            "ok": len(catalog["models"]) > 0,
            "total": len(catalog["models"]),
            "agents": len(catalog["agents"]),
            "selected": {a["agent_id"]: a["selected_model"] for a in catalog["agents"]
                         if a["selected_model"]},
        }
        return {
            "ok": all([
                checks["git"]["ok"], checks["database"]["ok"],
                checks["audit_chain"]["ok"], checks["context_bus"]["ok"],
                checks["agent_versions"]["ok"],
            ]),
            "checks": checks,
        }

    def close(self) -> None:
        self.db.close_thread()


def _count_by(items: list[Any], key) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        k = key(item)
        out[k] = out.get(k, 0) + 1
    return out
