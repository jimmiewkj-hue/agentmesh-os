"""Hub facade: one object wiring the control plane, adapters and persistence."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

from .adapters import DiscoveryContext, create_adapter, discover_agents
from .config import HubConfig, load_config
from .connectors import (
    CONNECTOR_PROFILES, TASK_CAPABILITY_REQUIREMENTS, connector_manifest,
    derive_capability_list, is_auto_executable,
)
from .errors import (
    ApprovalRequiredError, ConfigError, ConflictError, NotFoundError, ValidationError,
)
from .models import (AgentRecord, CAPABILITY_RANK, Project, Task, TaskSpec,
                     TaskStatus, TaskType, normalize_capability_level)
from .sessions.service import SessionImportService
from .storage.db import Database
from .storage.repo import Store
from .util import (
    atomic_write_text, ensure_dir, now_iso, read_json, read_text, sha1_hex, write_json,
)
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
from .control.model_profile import resolve_profile
from .control.paths import WorkspacePaths
from .control.planning import PlanningService
from .control.recovery import RecoveryService
from .control.router import CapabilityRouter
from .control.roles import RolePolicy
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
        self.store.project_id = self.project.project_id
        # Controller ownership: a second process must not release the leases of
        # a controller that is still heartbeating.
        self.instance_id = f"ctl_{uuid.uuid4().hex}"
        self.store.register_controller_instance(
            self.instance_id, self.project.project_id,
            host=socket.gethostname(), pid=os.getpid())
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="agentmesh-heartbeat", daemon=True)
        self._heartbeat_thread.start()
        # The bus is the single information path; Store writes automatically
        # fan out task/workflow/knowledge/file/governance/message/response events.
        self.bus = ContextBus(self.store, self.project, self.config)
        self.store.event_sink = self.bus
        try:
            self.store.drain_outbox()
        except Exception:
            pass
        self.audit = AuditService(self.store, self.project.project_id)
        self.budget = BudgetManager(self.store, self.project, self.config)
        self.approvals = ApprovalService(self.store, self.project, self.config)
        self.memory = MemoryService(self.store, self.project, self.config)
        try:
            previous_commit = self.store.get_setting("last_project_commit")
            if previous_commit and previous_commit != self.project.commit:
                self.memory.invalidate_on_baseline_change(
                    previous_commit, self.project.commit)
            self.store.set_setting("last_project_commit", self.project.commit)
        except Exception:
            pass
        self.context = ContextBuilder(self.store, self.project, self.config)
        self.router = CapabilityRouter(self.store, self.project, self.config)
        self.cost = CostOptimizer(self.store, self.project, self.config)
        # Role policy is loaded before the runner so an executing task can be
        # escalated along its role ladder without touching global state.
        try:
            self.roles = RolePolicy.load(self.root)
        except Exception as exc:  # a broken policy file must not brick the hub
            self.roles = RolePolicy.default()
            self.roles_error = str(exc)[:300]
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
            roles=self.roles, planner=self.planning,
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
        # An interrupted control plane leaves write leases behind; release the
        # ones whose run is no longer active so later tasks are not blocked by
        # a phantom scope conflict.
        self.runner.owner_id = self.instance_id
        self.runner.leases.owner_id = self.instance_id
        try:
            live = self.store.live_controller_ids(
                self.project.project_id, ttl_seconds=30.0, exclude=self.instance_id)
            self.stale_leases_released = self.runner.leases.release_stale(
                self.project.project_id, include_running=True, live_owners=live)
        except Exception:
            self.stale_leases_released = []
        self._migrate_legacy_budget()
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
        if bool(getattr(self.config, "auto_reconcile_on_startup", True)):
            threading.Thread(target=self._startup_reconcile,
                             name="agentmesh-startup-reconcile", daemon=True).start()

    def _migrate_legacy_budget(self) -> None:
        """1000000 was the old default; budgets are advisory now."""
        extra = dict(self.project.extra or {})
        try:
            stored = int(extra.get("token_budget") or 0)
        except (TypeError, ValueError):
            stored = 0
        if not self.config.budget_hard_stop and stored == 1_000_000:
            extra["token_budget"] = 0
            self.project.extra = extra
            self.store.save_project(self.project)
            self.audit.log("control-plane", "project.budget_migrated",
                           subject=self.project.project_id,
                           payload={"from": 1_000_000, "to": 0})

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

    def settings(self) -> dict[str, Any]:
        return {
            "full_control": bool(self.config.full_control),
            "multi_round": bool(self.config.multi_round),
            "max_rounds": int(self.config.max_rounds),
            "blocked_after_rounds": int(self.config.blocked_after_rounds),
            "round_replan": bool(self.config.round_replan),
        }

    def set_full_control(self, enabled: bool) -> dict[str, Any]:
        value = bool(enabled)
        self.config.full_control = value
        path = Path(self.project.root) / ".agentloop" / "config.json"
        data = read_json(path, {}) or {}
        if not isinstance(data, dict):
            data = {}
        data["full_control"] = value
        write_json(path, data, indent=2)
        self.audit.log("user", "settings.full_control", payload={"enabled": value,
                                                                 "path": str(path)})
        return {"full_control": value, "path": str(path)}

    def set_multi_round(self, enabled: bool, *, max_rounds: int | None = None) -> dict[str, Any]:
        value = bool(enabled)
        try:
            rounds = int(self.config.max_rounds if max_rounds is None else max_rounds)
        except (TypeError, ValueError):
            raise ValidationError("最大轮次必须是整数")
        if not 1 <= rounds <= 20:
            raise ValidationError("最大轮次必须在 1..20")
        self.config.multi_round = value
        self.config.max_rounds = rounds
        path = Path(self.project.root) / ".agentloop" / "config.json"
        data = read_json(path, {}) or {}
        if not isinstance(data, dict):
            data = {}
        data["multi_round"] = value
        data["max_rounds"] = rounds
        write_json(path, data, indent=2)
        self.audit.log("user", "settings.multi_round",
                       payload={"enabled": value, "max_rounds": rounds,
                                "path": str(path)})
        return {"multi_round": value, "max_rounds": rounds, "path": str(path)}

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
                # Discovery only knows what it can probe right now; a richer
                # catalog from the daily sync must not be shrunk by a restart.
                existing_models = {
                    row["model"] for row in self.store.list_agent_models(
                        self.project.project_id, agent_id=record.agent_id)
                }
                self.store.replace_discovered_models(
                    self.project.project_id, record.agent_id,
                    sorted(existing_models | set(record.models)),
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
                 permissions: dict[str, Any] | None = None, note: str = "",
                 context_length: int | None = None,
                 capability_level: str | None = None) -> dict[str, Any]:
        self.get_agent(agent_id)
        if model and (context_length is not None or capability_level is not None):
            self.set_agent_model(agent_id, model, context_length=context_length,
                                 capability_level=capability_level)
        self.store.set_main_node(self.project.project_id, agent_id, model,
                                 budget_tokens=budget_tokens, permissions=permissions, note=note)
        self.audit.log("user", "main_node.set", subject=agent_id,
                       payload={"model": model, "budget_tokens": budget_tokens,
                                "context_length": context_length,
                                "capability_level": capability_level})
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
        profile: dict[str, Any] = {}
        if agent:
            profiles = (agent.extra or {}).get("model_profiles") or {}
            candidate = profiles.get(row.get("model") or "")
            if isinstance(candidate, dict):
                profile = candidate
        return {
            **row,
            "context_length": int(profile.get("context_length") or 0),
            "capability_level": str(profile.get("capability_level") or ""),
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

    def set_agent_model(self, agent_id: str, model: str, *,
                        context_length: int | None = None,
                        capability_level: str | None = None) -> dict[str, Any]:
        result = self.model_catalog.select(
            agent_id, model, context_length=context_length,
            capability_level=capability_level)
        self.audit.log("user", "agent.model_selected", subject=agent_id,
                       payload={"model": result.get("selected_model", ""),
                                "context_length": result.get("context_length", 0),
                                "capability_level": result.get("capability_level", "")})
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

    def sync_models(self, agent_id: str = "") -> dict[str, Any]:
        """Refresh the agent list and the real model catalog (daily job)."""
        if not agent_id:
            try:
                self.discover(merge=True)
            except Exception:
                pass
        result = self.model_catalog.sync_all(agent_id=agent_id)
        info = {"synced_at": result["synced_at"], "agents": result["agents"]}
        try:
            write_json(Path(self.project.root) / ".agentloop" / "models-sync.json", info)
        except Exception:
            pass
        self.audit.log("control-plane", "models.synced", subject=agent_id or "all",
                       payload=info)
        return {"status": "synced", **info}

    def models_sync_status(self, *, max_age_hours: float = 24.0) -> dict[str, Any]:
        path = Path(self.project.root) / ".agentloop" / "models-sync.json"
        data = read_json(path, {}) or {}
        synced_at = str(data.get("synced_at") or "")
        age_hours = None
        if synced_at:
            try:
                from datetime import datetime, timezone
                stamp = datetime.fromisoformat(synced_at.replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - stamp).total_seconds() / 3600
            except Exception:
                age_hours = None
        return {"synced_at": synced_at, "agents": data.get("agents", []),
                "age_hours": None if age_hours is None else round(age_hours, 2),
                "stale": age_hours is None or age_hours >= float(max_age_hours),
                "max_age_hours": max_age_hours}

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

    def submit_result(self, uid: str, payload: dict[str, Any] | str | Path, *,
                      attempt_id: str = "", request_id: str = "") -> dict[str, Any]:
        return self.runner.submit_result(uid, payload, attempt_id=attempt_id,
                                         request_id=request_id)

    def submit_summary(self, run_id: str, payload: dict[str, Any] | str | Path) -> dict[str, Any]:
        return self.runner.submit_summary(run_id, payload)

    def cancel_run(self, run_id: str, reason: str = "用户停止运行") -> dict[str, Any]:
        return self.runner.cancel(run_id, reason=reason)

    def pause_run(self, run_id: str, reason: str = "用户暂停") -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise NotFoundError(f"运行不存在: {run_id}")
        if run["status"] not in ("running", "waiting_user"):
            raise ValidationError(f"运行当前状态 {run['status']}，只有 running/waiting_user 可以暂停")
        self.store.update_run(run_id, status="paused", error=reason)
        self.audit.log("user", "run.pause_requested", subject=run_id,
                       payload={"reason": reason})
        return {"status": "paused", "run_id": run_id, "reason": reason}

    def cancel_task(self, uid: str) -> dict[str, Any]:
        return self.runner.cancel_task(uid)

    def retry_task(self, uid: str) -> dict[str, Any]:
        return self.runner.retry_task(uid)

    def resume_interrupted(self, uid: str) -> dict[str, Any]:
        return self.runner.resume_interrupted(uid)

    def repair_task(self, uid: str) -> dict[str, Any]:
        return self.runner.repair_task(uid)

    def capture_artifacts(self, uid: str) -> dict[str, Any]:
        return self.runner.capture_artifacts(uid)

    def reconcile_task(self, uid: str) -> dict[str, Any]:
        return self.runner.reconcile_submission(uid)

    def reverify_task(self, uid: str) -> dict[str, Any]:
        return self.runner.reverify_task(uid)

    def update_task_binding(self, uid: str, *, agent_id: str = "", model: str = "",
                            context_length: int | None = None,
                            capability_level: str | None = None,
                            requeue: bool = False) -> dict[str, Any]:
        """Edit a chain node's agent/model/profile while the run is active."""
        task = self.store.get_task(uid)
        if not task:
            raise NotFoundError(f"任务不存在: {uid}")
        if task.status in ("completed", "canceled", "rejected"):
            raise ValidationError(f"任务已结束（{task.status}），不能再修改绑定")
        if self.runner._is_active(uid):
            raise ValidationError("该节点正在执行；请在节点结束/重试前修改")
        agent = self.get_agent(agent_id or task.assigned_agent or task.spec.suggested_agent)
        name = str(model or task.spec.model or "").strip()
        if not name:
            name = str((agent.extra or {}).get("selected_model") or
                       (agent.models[0] if agent.models else ""))
        known = [row["model"] for row in self.store.list_agent_models(
            self.project.project_id, agent_id=agent.agent_id)] or list(agent.models)
        if name and known and name not in known:
            raise ValidationError(
                f"{agent.agent_id} 的真实模型清单中没有 {name}",
                details={"available": sorted(known)[:40]},
            )
        effective_context = task.spec.context_length \
            if context_length is None else max(0, int(context_length))
        effective_level = task.spec.capability_level \
            if capability_level is None else str(capability_level or "").strip()
        validated = resolve_profile(
            agent, name, self.config.to_dict(),
            context_length=effective_context,
            capability_level=effective_level,
        )
        fields: dict[str, Any] = {
            "assigned_agent": agent.agent_id,
            "suggested_agent": agent.agent_id,
            "model": name,
            "context_length": int(validated["context_length"]),
            "capability_level": str(validated["capability_level"]),
        }
        self.store.update_task(uid, **fields)
        self.audit.log("user", "task.binding_updated", subject=uid, payload={
            "agent_id": agent.agent_id, "model": name,
            "context_length": fields["context_length"],
            "capability_level": fields["capability_level"],
            "requested": {"context_length": context_length,
                          "capability_level": capability_level},
        })
        if requeue:
            # A relay/manual wait has no result; switching to an executable
            # agent must re-dispatch the same attempt-1 session/worktree.
            if task.status in ("running", "verifying") or self.runner._is_active(uid):
                raise ValidationError("该节点正在执行，不能改绑后立即重新派发")
            existing = self.store.get_result(uid)
            if existing and existing.get("valid"):
                raise ConflictError("该节点已有成果包，不能改绑重跑")
            self.store.update_task(uid, attempts=0, error="")
            self.store.set_task_status(uid, "queued", force=True)
            self.audit.log("user", "task.reassigned_requeued", subject=uid,
                           payload={"agent_id": agent.agent_id, "model": name})
        refreshed = self.store.get_task(uid)
        return refreshed.to_dict() if refreshed else task.to_dict()

    def merge(self, run_id: str, *, approval_id: str = "", commit: bool = False) -> dict[str, Any]:
        return self.merge_service.merge(run_id, approval_id=approval_id, commit=commit)

    def recheck_merge(self, run_id: str, *, commit: bool = True) -> dict[str, Any]:
        return self.merge_service.recheck_merge(run_id, commit=commit)

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
        if existing.kind == "plan" and existing.plan_id:
            # Plan approval is atomic with run/task creation and validates the
            # ticket against the exact revision.  It must not be pre-decided by
            # the generic path.
            if approve:
                result = self.planning.approve_plan(existing.plan_id,
                                                    approval_id=approval_id, note=note)
                approval = self.store.get_approval(approval_id) or existing
            else:
                approval = self.approvals.decide(approval_id, approved=False, note=note)
                self.planning.reject_plan(existing.plan_id, approval_id=approval_id, note=note)
                result = {"status": "rejected", "plan_id": existing.plan_id}
            return {"approval": approval.to_dict(), "outcome": result}
        approval = self.approvals.decide(approval_id, approved=approve, note=note)
        if approval.kind == "high_risk" and approve and approval.task_uid:
            task = self.store.get_task(approval.task_uid)
            if task and task.status == TaskStatus.WAITING_USER.value:
                self.store.set_task_status(task.uid, TaskStatus.QUEUED.value,
                                           error="", force=True)
                self.store.append_audit(
                    self.project.project_id, "user", "approval.requeued_task",
                    subject=task.uid,
                    payload={"approval_id": approval_id, "task_id": task.task_id},
                )
                # A persisted reconcile todo makes the wake survive a crash;
                # schedule_reconcile also tries to start the controller now.
                self.runner.schedule_reconcile(task.run_id, "high_risk_approved")
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
        result = self.recovery.recover(run_id=run_id, auto_resume=auto_resume)
        for target in result.get("runs_to_start") or []:
            try:
                result.setdefault("started", []).append(
                    self.runner.start(target, background=True))
            except Exception as exc:  # recovery must report, not crash the caller
                result.setdefault("start_errors", []).append(
                    {"run_id": target, "error": str(exc)[:300]})
        return result

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

    def bus_ack(self, subscription_id: str, seq: int) -> dict[str, Any]:
        return self.bus.ack(subscription_id, seq)

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
    # Role routing: role policy + deterministic segmentation + solve loop
    # ------------------------------------------------------------------
    def role_catalog(self) -> dict[str, list[str]]:
        """agent_id -> real model names (the only universe a policy may pick from)."""
        return {a.agent_id: list(a.models) for a in self.list_agents()}

    def role_policy(self) -> dict[str, Any]:
        report = self.roles.explain(agents=self.list_agents(), catalog=self.role_catalog())
        report["error"] = getattr(self, "roles_error", "")
        return report

    def resolve_role(self, role: str, *, limit: int = 6,
                     executor_agent: str = "", executor_model: str = "",
                     commander_agent: str = "", commander_model: str = "") -> dict[str, Any]:
        policy = self._session_policy(executor_agent, executor_model,
                                      commander_agent, commander_model)
        candidates = policy.candidates(role, agents=self.list_agents(),
                                       catalog=self.role_catalog(), limit=limit)
        return {
            "role": role,
            "policy": policy.name,
            "candidates": [c.to_dict() for c in candidates],
            "resolved": bool(candidates),
        }

    def _session_policy(self, executor_agent: str = "", executor_model: str = "",
                        commander_agent: str = "", commander_model: str = "") -> RolePolicy:
        catalog = self.role_catalog()
        if executor_agent and executor_model:
            self._require_real_model(executor_agent, executor_model, "执行模型")
        if commander_agent and commander_model:
            self._require_real_model(commander_agent, commander_model, "统帅模型")
        return self.roles.with_overrides(
            executor=(executor_agent, executor_model),
            commander=(commander_agent, commander_model),
            catalog=catalog,
        )

    def _require_real_model(self, agent_id: str, model: str, label: str) -> None:
        agent = self.store.get_agent(agent_id)
        if not agent:
            raise ValidationError(f"{label}指定的 Agent 不存在: {agent_id}")
        catalog = self.role_catalog().get(agent_id) or []
        known = [m.lower() for m in catalog]
        if model.lower() not in known:
            raise ValidationError(
                f"{agent_id} 的真实模型清单中没有 {model}（{label}）",
                details={"available": sorted(catalog)[:40]},
            )

    def import_role_policy(self, path: str | Path, *, save: bool = False) -> dict[str, Any]:
        """Import OpenCode ``agent`` blocks or a DSH-style ``AGENTS.md``."""
        from .control.roles import policy_from_agents_md, policy_from_opencode

        target = Path(path).expanduser()
        if not target.exists():
            raise NotFoundError(f"配置文件不存在: {target}")
        catalog = self.role_catalog()
        if target.suffix.lower() in (".md", ".markdown", ".txt"):
            parsed = policy_from_agents_md(target, catalog=catalog)
            kind = "agents_md"
        else:
            parsed = policy_from_opencode(target, catalog=catalog)
            kind = "opencode"
        overlay = {"roles": parsed.get("roles") or {}}
        merged = self.roles.merged(overlay, source=f"{self.roles.source}+{target.name}")
        result = {
            "kind": kind, "source": str(target),
            "roles_found": parsed.get("roles_found") or [],
            "matched": parsed.get("matched") or [],
            "unmatched": parsed.get("unmatched") or [],
            "policy": merged.name,
            "saved": "",
        }
        if save:
            out_path = self.root / ".agentloop" / "roles.json"
            merged.save(out_path)
            self.roles = merged
            self.runner.roles = merged
            result["saved"] = str(out_path)
        self.audit.log("user", "roles.imported", payload={
            "source": str(target), "kind": kind, "saved": result["saved"],
            "matched": len(result["matched"]), "unmatched": len(result["unmatched"]),
        })
        return result

    def save_role_policy(self, path: str = "") -> dict[str, Any]:
        out_path = Path(path).expanduser() if path else self.root / ".agentloop" / "roles.json"
        saved = self.roles.save(out_path)
        self.audit.log("user", "roles.saved", subject=str(out_path), payload=saved)
        return saved

    def segment_goal(self, goal: str, *, max_tasks: int = 4,
                     executor_agent: str = "", executor_model: str = "",
                     commander_agent: str = "", commander_model: str = "") -> dict[str, Any]:
        """Show the deterministic split without running anything."""
        from .control.segment import Segmenter

        policy = self._session_policy(executor_agent, executor_model,
                                      commander_agent, commander_model)
        segmenter = Segmenter(policy, max_tasks=max_tasks,
                              test_command=self.project.test_command)
        segment = segmenter.segment(goal)
        catalog = self.role_catalog()
        agents = self.list_agents()
        rows = []
        for spec in segment.tasks:
            candidates = policy.candidates(spec.role, agents=agents, catalog=catalog, limit=3)
            rows.append({
                "task": spec.to_dict(),
                "candidates": [c.to_dict() for c in candidates],
            })
        payload = segment.to_dict()
        payload["bindings"] = rows
        return payload

    def solve(self, goal: str, *, executor_agent: str = "", executor_model: str = "",
              commander_agent: str = "", commander_model: str = "",
              max_tasks: int = 4, max_parallel: int = 1, start: bool = True,
              background: bool = True, polish: bool = False,
              approve: bool = False) -> dict[str, Any]:
        """One request in, a routed multi-model run (and a final answer) out.

        Steps: segment -> resolve each role against the real catalog -> pin
        (agent, model) per task -> auto-approve architecture/plan -> run ->
        aggregate. Budget rules from the policy are applied at planning time,
        so an over-budget run degrades *before* spending.
        """
        from .control.segment import Segmenter

        policy = self._session_policy(executor_agent, executor_model,
                                      commander_agent, commander_model)
        segmenter = Segmenter(policy, max_tasks=max_tasks,
                              test_command=self.project.test_command)
        segment = segmenter.segment(goal)
        agents = self.list_agents()
        catalog = self.role_catalog()
        premium_used = 0
        agent_used: dict[str, int] = {}
        executor_agents: set[str] = set()
        bindings: list[dict[str, Any]] = []
        notes: list[str] = list(segment.notes)
        for spec in segment.tasks:
            # Independent verification must not be the agent that did the work.
            exclude = executor_agents if spec.role == "verify" else set()
            candidates = policy.candidates(spec.role, agents=agents, catalog=catalog,
                                           exclude=exclude)
            if not candidates:
                notes.append(f"{spec.role}: 没有可用候选，任务将交给通用路由")
                bindings.append({"task_id": spec.task_id, "role": spec.role,
                                 "agent_id": "", "model": "", "tier": None,
                                 "reason": "no_candidate"})
                continue
            chosen = None
            skipped: list[str] = []
            for candidate in candidates:
                limit = policy.limits_for(candidate.agent_id).get("max_tasks_per_run")
                if limit is not None and agent_used.get(candidate.agent_id, 0) >= int(limit):
                    skipped.append(f"{candidate.agent_id} 超过每轮 {limit} 个任务上限")
                    continue
                if policy.is_premium(candidate.tier) \
                        and premium_used >= policy.max_premium_tasks_per_run:
                    skipped.append(f"premium 档预算 {policy.max_premium_tasks_per_run} 已用完")
                    continue
                chosen = candidate
                break
            downgraded = False
            if chosen is None:
                chosen = min(candidates, key=lambda c: (c.tier, c.agent_id))
                downgraded = True
                notes.append(f"{spec.role}: 预算/限额已满，降级到 tier {chosen.tier}"
                             f"（{chosen.agent_id}/{chosen.model}）：{'；'.join(skipped[:3])}")
            spec.suggested_agent = chosen.agent_id
            spec.model = chosen.model
            agent_used[chosen.agent_id] = agent_used.get(chosen.agent_id, 0) + 1
            if spec.writable:
                executor_agents.add(chosen.agent_id)
            if policy.is_premium(chosen.tier):
                premium_used += 1
            bindings.append({
                "task_id": spec.task_id, "role": spec.role, "type": spec.type,
                "agent_id": chosen.agent_id, "agent_name": chosen.agent_name,
                "model": chosen.model, "tier": chosen.tier, "tier_label": chosen.tier_label,
                "reason": chosen.why, "downgraded": downgraded,
            })
        plan_payload = {
            "plan_version": 1,
            "goal": goal,
            "constraints": ["只允许通过各 Agent 自身的软件接口执行，禁止 OS 键鼠合成"],
            "tasks": [spec.to_dict() for spec in segment.tasks],
        }
        if not commander_agent and not self.store.get_main_node(self.project.project_id):
            picked = self.auto_main()
            chosen_main = (picked.get("selected") or {}).get("agent_id", "")
            notes.append(f"未指定统帅模型，已自动选择主节点 {chosen_main}（只做记录，不参与执行）")
        imported = self.import_plan(
            plan_payload,
            main_agent_id=commander_agent or "",
            model=commander_model or "",
        )
        if not approve:
            # Batch/one-shot callers must opt into approval explicitly; the
            # plan and its approval stay visible and pending otherwise.
            return {
                "status": "pending_approval", "plan_id": imported["plan_id"],
                "approval_id": imported.get("approval_id", ""),
                "architecture_approval_id": imported.get("architecture_approval_id", ""),
                "goal": goal, "run_id": "", "confidence": segment.confidence,
                "tasks": [spec.to_dict() for spec in segment.tasks],
                "bindings": bindings, "notes": notes,
                "premium_tasks": premium_used,
                "premium_budget": policy.max_premium_tasks_per_run,
                "next": "批准计划后调用 start_run",
            }
        architecture_id = imported.get("architecture_approval_id") or ""
        if architecture_id:
            self.decide_approval(architecture_id, approve=True)
        plan_approval = str(imported.get("approval_id") or "")
        approved = self.decide_approval(plan_approval, approve=True)
        run_id = approved["outcome"]["run_id"]
        for spec in segment.tasks:
            binding = next((b for b in bindings if b["task_id"] == spec.task_id), None)
            if binding and binding["agent_id"]:
                self.store.update_task(f"{run_id}:{spec.task_id}",
                                       assigned_agent=binding["agent_id"],
                                       model=binding["model"])
        self.audit.log("user", "solve.configured", subject=run_id, payload={
            "goal": goal[:500], "tasks": len(segment.tasks),
            "confidence": segment.confidence,
            "bindings": [{k: b.get(k) for k in ("task_id", "role", "agent_id", "model", "tier")}
                         for b in bindings],
            "premium_tasks": premium_used,
        })
        if self.bus:
            self.bus.publish("workflow", {
                "phase": "solve_planned", "run_id": run_id, "goal": goal,
                "tasks": len(segment.tasks), "premium_tasks": premium_used,
                "bindings": bindings,
            }, correlation_id=run_id, sync_level=5)
        result = {
            "status": "planned", "run_id": run_id, "plan_id": imported["plan_id"],
            "goal": goal, "confidence": segment.confidence,
            "tasks": [spec.to_dict() for spec in segment.tasks],
            "bindings": bindings, "notes": notes,
            "premium_tasks": premium_used,
            "premium_budget": policy.max_premium_tasks_per_run,
        }
        if start:
            self.start_run(run_id, max_parallel=max_parallel, background=background)
            result["status"] = "running" if background else "finished"
            if polish:
                result["answer"] = self.aggregate_run(run_id, polish=True)
            else:
                result["answer"] = self.aggregate_run(run_id)
        return result

    def aggregate_run(self, run_id: str = "", *, polish: bool = False) -> dict[str, Any]:
        """Deterministic aggregation of every task result into one answer."""
        from .control.aggregate import apply_polish, build_answer, build_polish_prompt

        run = self.store.get_run(run_id) if run_id else self.latest_run()
        if not run:
            raise NotFoundError("没有可汇总的运行")
        plan = self.store.get_plan(run["plan_id"])
        tasks = self.store.list_tasks(run["run_id"])
        results: dict[str, dict[str, Any]] = {}
        for task in tasks:
            row = self.store.get_result(task.uid)
            if row and isinstance(row.get("payload"), dict):
                results[task.task_id] = row["payload"]
        answer = build_answer(
            goal=plan.goal if plan else "",
            run=run,
            plan=plan.to_dict() if plan else None,
            tasks=[t.to_dict() for t in tasks],
            results=results,
        )
        if polish:
            answer = self._polish_answer(run["run_id"], answer, build_polish_prompt)
        directory = self.paths.run_dir(run["run_id"])
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / "answer.json", answer)
        atomic_write_text(directory / "answer.md", answer["answer_markdown"])
        if self.bus:
            self.bus.publish("response", {
                "purpose": "aggregate", "run_id": run["run_id"],
                "status": run.get("status", ""), "tasks": len(tasks),
                "premium_tasks": answer["cost"]["premium_tasks"],
                "tokens": answer["cost"]["total_tokens"],
                "polished": answer.get("polished", False),
                "answer_path": str(directory / "answer.md"),
            }, correlation_id=run["run_id"])
        return answer

    def _polish_answer(self, run_id: str, answer: dict[str, Any], prompt_builder) -> dict[str, Any]:
        """Optional: let the summarizer role rewrite the deterministic digest.

        Any failure keeps the deterministic answer — polishing is an upgrade,
        never a dependency.
        """
        from .adapters import create_adapter
        from .adapters.base import ExecutionRequest
        from .control.aggregate import apply_polish
        from .control.roles import classify_model

        policy = self.roles
        if "summarize" not in policy.roles:
            return answer
        candidates = policy.candidates("summarize", agents=self.list_agents(),
                                       catalog=self.role_catalog(), limit=1)
        if not candidates:
            return answer
        chosen = candidates[0]
        agent = self.store.get_agent(chosen.agent_id)
        if not agent:
            return answer
        directory = self.paths.run_dir(run_id) / "aggregate"
        directory.mkdir(parents=True, exist_ok=True)
        prompt_path = directory / "polish_request.md"
        output_path = directory / "polish.md"
        atomic_write_text(prompt_path, prompt_builder(answer))
        synthetic = Task(
            uid=f"{run_id}:AGGREGATE", project_id=self.project.project_id,
            plan_id="", run_id=run_id,
            spec=TaskSpec(
                task_id="AGGREGATE", title="汇总", objective=answer.get("conclusion", ""),
                type="analysis", role="summarize", model=chosen.model,
                token_budget=8000, time_limit_minutes=10, retry_limit=0,
            ),
            status="running", assigned_agent=chosen.agent_id, base_commit=self.project.commit,
        )
        request = ExecutionRequest(
            project_id=self.project.project_id, project_root=self.project.root,
            task=synthetic, workdir=str(self.root), task_dir=str(directory),
            prompt_path=str(prompt_path), result_path=str(output_path),
            model=chosen.model, timeout_seconds=self.config.summary_timeout_seconds,
            purpose="aggregate", full_control=bool(self.config.full_control),
        )
        try:
            outcome = create_adapter(agent).execute(request)
        except Exception as exc:  # adapter/agent problem must not lose the answer
            self.audit.log("control-plane", "aggregate.polish_failed",
                           subject=run_id, payload={"error": str(exc)[:300]})
            return answer
        text = read_text(output_path, default="") or (outcome.text or "")
        if outcome.status != "completed" or not text.strip():
            self.audit.log("control-plane", "aggregate.polish_failed", subject=run_id,
                           payload={"status": outcome.status, "error": outcome.error[:300]})
            return answer
        self.audit.log("control-plane", "aggregate.polished", subject=run_id, payload={
            "agent": chosen.agent_id, "model": chosen.model,
            "tier": chosen.tier, "model_class": classify_model(chosen.model),
        })
        return apply_polish(answer, text)

    def solve_status(self, run_id: str = "") -> dict[str, Any]:
        run = self.store.get_run(run_id) if run_id else self.latest_run()
        if not run:
            return {"status": "no_run", "answer": None}
        tasks = self.store.list_tasks(run["run_id"])
        done = all(t.status in ("completed", "failed", "canceled", "rejected") for t in tasks)
        answer = self.aggregate_run(run["run_id"]) if done and tasks else None
        return {
            "status": run.get("status", ""), "run_id": run["run_id"],
            "acceptance_status": run.get("acceptance_status", ""),
            "tasks": [{k: t.to_dict()[k] for k in
                       ("task_id", "role", "model", "assigned_agent", "status",
                        "verification_status", "error")} for t in tasks],
            "answer": answer,
        }

    # ------------------------------------------------------------------
    # Multi-agent chain (workspace + per-stage agent/model pins)
    # ------------------------------------------------------------------
    def configure_chain(self, *, goal: str, stages: list[dict[str, Any]],
                        main_agent_id: str = "", start: bool = False,
                        background: bool = True, max_parallel: int = 1,
                        approve: bool = True, plan_id: str = "",
                        save_only: bool = False) -> dict[str, Any]:
        """Pin one agent + one real model per stage of a chain.

        Two modes exist on purpose:

        * ``plan_id`` given: the stages are bound onto an existing
          ``pending_approval`` plan.  Nothing is approved or started here; the
          plan approval in step 4 remains the single execution gate.
        * no ``plan_id``: a standalone chain plan is created.  Unless the
          caller explicitly asks for ``approve``/``start`` it stays pending and
          uses the same step-4 gate.

        Each stage's model is validated against the agent's own discovered
        catalog before anything is stored.  ``assigned_agent`` is written on
        the task row so the capability router cannot silently substitute a
        different agent.
        """
        if not goal.strip() and not plan_id:
            raise ValidationError("链路目标不能为空")
        if not stages:
            raise ValidationError("链路至少需要一个阶段")
        prepared = [self._prepare_chain_stage(raw, index)
                    for index, raw in enumerate(stages, start=1)]
        # A model is a property of the task, not of the agent: the executor
        # reads ``task.spec.model`` first and only falls back to the agent's
        # selected model.
        pinned: dict[str, str] = {}
        for stage in prepared:
            pinned[stage["agent_id"]] = stage["model"]
        for agent_id, model in pinned.items():
            self.set_agent_model(agent_id, model)

        if plan_id:
            plan = self.store.get_plan(plan_id)
            if not plan:
                raise NotFoundError(f"计划不存在: {plan_id}")
            if plan.status != "pending_approval":
                raise ValidationError(
                    f"计划状态为 {plan.status}，只有待审批计划可以绑定链路配置",
                    details={"plan_id": plan_id, "status": plan.status},
                )
            prepared = self._bind_chain_to_plan(plan, prepared)
            self.store.save_plan(plan)
            plan_approval = self.approvals.ensure_plan_approval(plan)
            self.audit.log("user", "chain.bound", subject=plan_id, payload={
                "goal": plan.goal,
                "plan_revision": plan.revision,
                "plan_hash": plan.content_hash,
                "approval_id": plan_approval.approval_id,
                "stages": [{"task_id": s["task_id"], "agent": s["agent_id"],
                            "model": s["model"], "type": s["type"]} for s in prepared],
            })
            outcome: dict[str, Any] = {
                "status": "bound", "plan_id": plan_id, "run_id": "",
                "main_agent_id": main_agent_id or plan.main_agent_id,
                "approval_id": plan_approval.approval_id,
                "plan_revision": plan.revision,
                "plan_hash": plan.content_hash,
                "stages": prepared,
                "next": "到第 4 步批准计划，再点击开始执行",
            }
            if approve and not save_only:
                for item in self.approvals.pending():
                    if item.kind == "architecture" and item.plan_id == plan_id:
                        self.decide_approval(item.approval_id, approve=True)
                approval_id = plan_approval.approval_id
                approved = self.decide_approval(approval_id, approve=True)
                run_id = approved["outcome"]["run_id"]
                for stage in prepared:
                    self.store.update_task(f"{run_id}:{stage['task_id']}",
                                           assigned_agent=stage["agent_id"],
                                           model=stage["model"])
                outcome.update({"status": "approved", "run_id": run_id,
                                "approval_id": approval_id})
                if start:
                    self.start_run(run_id, max_parallel=max_parallel,
                                   background=background)
                    outcome["status"] = "started"
            return outcome

        # ------------------------------------------------------------------
        # Standalone chain: build a plan; approval remains explicit unless the
        # caller opted into the legacy one-shot behaviour.
        # ------------------------------------------------------------------
        default_write_scope = ["tests/**", "test_*.py"]
        tasks: list[dict[str, Any]] = []
        previous = ""
        for index, stage in enumerate(prepared, start=1):
            task_id = f"CHAIN{index:02d}"
            if stage["type"] in ("implementation", "documentation") and not stage["scope"]:
                stage["scope"] = list(default_write_scope)
                stage["scope_source"] = "chain-default-tests"
            objective = stage["objective"] or (
                f"{goal}（第 {index} 阶段：{stage['agent_name']}）"
            )
            tasks.append({
                "task_id": task_id,
                "title": stage["title"] or f"阶段 {index} · {stage['agent_name']}",
                "type": stage["type"],
                "role": str(stage.get("role") or ""),
                "model": stage["model"],
                "objective": objective,
                "suggested_agent": stage["agent_id"],
                "dependencies": [previous] if previous else [],
                "scope": stage["scope"],
                "out_of_scope": stage["out_of_scope"],
                "expected_outputs": ["summary"],
                "acceptance_criteria": ["结构化成果包 + 可定位证据"],
                "verification_commands": stage["verification_commands"],
                "token_budget": int(stage["token_budget"]),
                "time_limit_minutes": int(stage["time_limit_minutes"]),
                "retry_limit": int(stage["retry_limit"]),
                "risk_level": "low",
                "priority": max(0, 9 - index),
                "approval_policy": "auto",
            })
            stage["task_id"] = task_id
            previous = task_id
        plan_payload = {
            "plan_version": 1,
            "goal": goal,
            "constraints": ["只允许通过各 Agent 自身的软件接口执行，禁止 OS 键鼠合成"],
            "tasks": tasks,
        }
        imported = self.import_plan(plan_payload)
        # The collaboration contract in import_plan may reassign roles to the
        # main/selected sub-agent. A 3.5 chain is an explicit human pin, so the
        # per-stage agent/model/type must win over that automatic contract.
        imported_plan = self.store.get_plan(imported["plan_id"])
        if imported_plan is not None:
            prepared = self._bind_chain_to_plan(imported_plan, prepared)
            self.store.save_plan(imported_plan)
            # Binding changed executable content, so the ticket created during
            # import is expired by save_plan.  Approve the new revision only.
            plan_approval = self.approvals.ensure_plan_approval(imported_plan)
            imported["approval_id"] = plan_approval.approval_id
            self.audit.log("user", "chain.pins_applied", subject=imported_plan.plan_id,
                           payload={"plan_revision": imported_plan.revision,
                                    "plan_hash": imported_plan.content_hash,
                                    "approval_id": plan_approval.approval_id,
                                    "stages": [
                                        {"task_id": s["task_id"], "agent": s["agent_id"],
                                         "model": s["model"], "type": s["type"]}
                                        for s in prepared]})
        outcome = {
            "status": "pending_approval", "plan_id": imported["plan_id"],
            "run_id": "", "main_agent_id": main_agent_id or "",
            "stages": prepared, "approval_id": imported.get("approval_id", ""),
            "architecture_approval_id": imported.get("architecture_approval_id", ""),
            "next": "到第 4 步批准计划，再点击开始执行",
        }
        if save_only or not approve:
            self.audit.log("user", "chain.saved", subject=imported["plan_id"], payload={
                "goal": goal,
                "stages": [{"task_id": s["task_id"], "agent": s["agent_id"],
                            "model": s["model"], "type": s["type"]} for s in prepared],
                "next": "批准计划后开始执行",
            })
            return outcome
        architecture_id = imported.get("architecture_approval_id") or ""
        if architecture_id:
            self.decide_approval(architecture_id, approve=True)
        plan_approval = str(imported.get("approval_id") or "")
        approved = self.decide_approval(plan_approval, approve=True)
        run_id = approved["outcome"]["run_id"]
        for stage in prepared:
            self.store.update_task(f"{run_id}:{stage['task_id']}",
                                   assigned_agent=stage["agent_id"],
                                   model=stage["model"])
        self.audit.log("user", "chain.configured", subject=run_id, payload={
            "goal": goal,
            "stages": [{"task_id": s["task_id"], "agent": s["agent_id"],
                        "model": s["model"], "type": s["type"]} for s in prepared],
        })
        outcome.update({"status": "configured", "run_id": run_id})
        if start:
            self.start_run(run_id, max_parallel=max_parallel, background=background)
            outcome["status"] = "started"
        return outcome

    def _prepare_chain_stage(self, raw: dict[str, Any], index: int) -> dict[str, Any]:
        agent_id = str(raw.get("agent_id") or "").strip()
        model = str(raw.get("model") or "").strip()
        if not agent_id:
            raise ValidationError(f"第 {index} 阶段缺少 agent_id")
        agent = self.get_agent(agent_id)
        if not model:
            model = str((agent.extra or {}).get("selected_model") or
                        (agent.models[0] if agent.models else ""))
        known = [row["model"] for row in self.store.list_agent_models(
            self.project.project_id, agent_id=agent_id)]
        if not known:
            known = list(agent.models)
        if not known:
            raise ValidationError(
                f"{agent_id} 没有可验证的模型清单；请先 discover/models --refresh"
            )
        if model not in known:
            raise ValidationError(
                f"{agent_id} 的真实模型清单中没有 {model}",
                details={"available": sorted(known)[:40]},
            )
        stage_type = str(raw.get("type") or
                         {1: "analysis", 2: "review", 3: "implementation"}.get(
                             index, "analysis"))
        legal_types = {t.value for t in TaskType}
        if stage_type not in legal_types:
            raise ValidationError(
                f"未知任务类型 {stage_type}",
                details={"allowed": sorted(legal_types)},
            )
        return {
            "stage": index,
            "task_id": str(raw.get("task_id") or "").strip(),
            "agent_id": agent_id,
            "agent_name": agent.name,
            "model": model,
            "type": stage_type,
            "role": str(raw.get("role") or ""),
            "objective": str(raw.get("objective") or "").strip(),
            "title": str(raw.get("title") or "").strip(),
            "scope": list(raw.get("scope") or []),
            "out_of_scope": list(raw.get("out_of_scope") or []),
            "verification_commands": list(raw.get("verification_commands") or []),
            "time_limit_minutes": int(raw.get("time_limit_minutes") or 30),
            "retry_limit": int(raw.get("retry_limit", 1)),
            "token_budget": int(raw.get("token_budget") or 60000),
        }

    def _bind_chain_to_plan(self, plan: Any,
                            prepared: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Write per-stage agent/model/type onto an existing pending plan.

        The plan owns the task graph; step 3.5 only pins who runs each node.
        Stage order and task count must match so a stale browser draft cannot
        silently retarget a different plan.
        """
        by_id = {task.task_id: task for task in plan.tasks}
        user_tasks = [task for task in plan.tasks
                      if str(getattr(task, "origin", "user") or "user") != "system"]
        explicit = [bool(stage.get("task_id")) for stage in prepared]
        if any(explicit) and not all(explicit):
            raise ValidationError("链路阶段必须全部指定 task_id，或全部按顺序绑定")
        if all(explicit):
            targets = []
            for stage in prepared:
                spec = by_id.get(str(stage["task_id"]))
                if spec is None:
                    raise ValidationError(f"计划中没有任务 {stage['task_id']}")
                if str(getattr(spec, "origin", "user") or "user") == "system":
                    raise ValidationError(
                        f"系统治理节点 {spec.task_id} 不能绑定为链路阶段")
                targets.append(spec)
            missing = [task.task_id for task in user_tasks if task not in targets]
            if missing:
                raise ValidationError(
                    f"阶段数量与用户任务数不一致，缺少: {missing}",
                    details={"plan_id": plan.plan_id, "missing": missing})
        else:
            if len(prepared) != len(user_tasks):
                raise ValidationError(
                    f"阶段数量（{len(prepared)}）与用户任务数（{len(user_tasks)}）不一致",
                    details={"plan_id": plan.plan_id,
                             "user_tasks": [t.task_id for t in user_tasks],
                             "system_tasks": [t.task_id for t in plan.tasks
                                              if t not in user_tasks]},
                )
            targets = list(user_tasks)
        for stage, spec in zip(prepared, targets):
            if not stage["task_id"]:
                stage["task_id"] = spec.task_id
            old = {"agent": spec.suggested_agent, "model": spec.model,
                   "type": spec.type}
            spec.suggested_agent = stage["agent_id"]
            spec.model = stage["model"]
            spec.type = stage["type"]
            if spec.type in ("implementation", "documentation") and not spec.scope:
                spec.scope = ["tests/**", "test_*.py"]
            stage.update({"title": spec.title, "objective": spec.objective,
                          "role": spec.role, "previous": old})
        plan.validate()
        return prepared

    # ------------------------------------------------------------------
    # Preflight: open the selected agents and bind workspace + model
    # ------------------------------------------------------------------
    def preflight_agents(self, stages: list[dict[str, Any]], *,
                         workspace: str = "", open_apps: bool = False,
                         full_control: bool = True,
                         config: dict[str, Any] | None = None) -> dict[str, Any]:
        """Open every selected agent and verify its workspace + model.

        Trae and Qoder are actually relaunched with their programmatic
        interface and read back; CLI agents are checked for executable and
        catalog membership.  This is deliberately a separate, explicit step:
        it can restart desktop apps and therefore never runs implicitly.
        """
        from .control import trae_interface
        from .control.interfaces import (
            prepare_qoder_session, qoder_model_key, verify_qoder_session,
        )
        from .connectors import is_auto_executable
        from .gui.permissions import ensure_trae_full_access

        root = str(Path(workspace).expanduser().resolve()) if workspace else \
            str(self.project.root)
        ensure_dir(Path(root))
        report: list[dict[str, Any]] = []
        for stage in stages:
            agent_id = str(stage.get("agent_id") or "")
            model = str(stage.get("model") or "")
            agent = self.get_agent(agent_id)
            cfg = dict(agent.adapter_config or {})
            cfg.update(config or {})
            item: dict[str, Any] = {"agent_id": agent_id, "name": agent.name,
                                    "model": model, "workspace": root,
                                    "adapter": agent.adapter_type}
            known = [row["model"] for row in self.store.list_agent_models(
                self.project.project_id, agent_id=agent_id)] or list(agent.models)
            if model not in known:
                item.update({"ok": False, "stage": "model",
                             "error": f"{agent_id} 的真实模型清单中没有 {model}",
                             "available": sorted(known)[:20]})
                report.append(item)
                continue
            app = str(cfg.get("gui_app") or "")
            try:
                if app in ("trae_cn", "trae"):
                    cdp = trae_interface.ensure_trae_cdp(
                        app=app, port=int(cfg.get("trae_cdp_port")
                                          or trae_interface.CDP_DEFAULT_PORT),
                        workspace=root, executable=str(cfg.get("executable") or ""),
                        config=cfg, allow_restart=open_apps, force_restart=False,
                        before_launch=lambda: trae_interface.set_trae_workspace(
                            workspace=root, app=app, config=cfg))
                    if not cdp.ok:
                        item.update({"ok": False, "stage": "launch", "error": cdp.reason})
                        report.append(item)
                        continue
                    ws = trae_interface.set_trae_workspace(
                        workspace=root, app=app,
                        dev_mode=str(cfg.get("trae_dev_mode") or "code"), config=cfg)
                    md = trae_interface.set_trae_model(
                        model=model, app=app, config=cfg,
                        cdp_port=int(cfg.get("trae_cdp_port")
                                     or trae_interface.CDP_DEFAULT_PORT))
                    verify = trae_interface.verify_trae_session(
                        workspace=root, model=model,
                        dev_mode=str(cfg.get("trae_dev_mode") or "code"),
                        app=app, config=cfg,
                        cdp_port=int(cfg.get("trae_cdp_port")
                                     or trae_interface.CDP_DEFAULT_PORT))
                    full_access = {}
                    if full_control:
                        try:
                            full_access = ensure_trae_full_access(
                                port=int(cfg.get("trae_cdp_port")
                                         or trae_interface.CDP_DEFAULT_PORT))
                        except Exception as exc:
                            full_access = {"ok": False, "reason": str(exc)[:300]}
                    item.update({
                        "ok": bool(ws.ok and md.ok and verify.ok),
                        "stage": "ready" if verify.ok else "verify",
                        "process": "running" if cdp.ok else "not_running",
                        "visible_model": md.actual.get("visible_model", ""),
                        "workspace_set": ws.ok, "model_set": md.ok,
                        "full_access": full_access,
                        "verify": verify.to_dict(),
                        "error": "" if verify.ok else verify.reason,
                    })
                elif app == "qoder_cn":
                    qkey = qoder_model_key(model)
                    if not open_apps:
                        # Read-only preflight: verify the existing binding and
                        # never stop the app or migrate the user's transcript.
                        verify = verify_qoder_session(
                            workspace=root, model=model, started_at=0,
                            config=cfg, strict_daemon=False)
                        item.update({
                            "ok": verify.ok, "stage": "read_only",
                            "read_only": True, "workspace_set": verify.ok,
                            "model_set": verify.ok, "verify": verify.to_dict(),
                            "error": "" if verify.ok else verify.reason,
                        })
                        report.append(item)
                        continue
                    prep = prepare_qoder_session(
                        workspace=root, model=qkey, config=cfg,
                        permission_mode=("bypassPermissions" if full_control else ""),
                        allow_restart=open_apps,
                        allow_history_migration=open_apps,
                    )
                    if not prep.ok:
                        item.update({"ok": False, "stage": "launch", "error": prep.reason})
                        report.append(item)
                        continue
                    verify = verify_qoder_session(
                        workspace=root, model=model, started_at=time.time() - 2,
                        config=cfg, strict_daemon=False,
                        session_id=str(prep.evidence.get("session_id") or ""))
                    item.update({
                        "ok": verify.ok, "stage": "ready" if verify.ok else "verify",
                        "process": "running", "workspace_set": True, "model_set": True,
                        "verify": verify.to_dict(),
                        "error": "" if verify.ok else verify.reason,
                    })
                elif app == "workbuddy":
                    # Opening the renderer with the debugging switch is the
                    # only preflight WorkBuddy needs; workspace/model are bound
                    # by the task deeplink at execution time.
                    from .control.interfaces import ensure_workbuddy_cdp
                    cdp = ensure_workbuddy_cdp(
                        executable=str(cfg.get("executable") or ""),
                        port=int(cfg.get("workbuddy_cdp_port") or 9223),
                        allow_restart=open_apps, allow_launch=open_apps,
                    )
                    item.update({
                        "ok": cdp.ok, "stage": "ready" if cdp.ok else "launch",
                        "process": "running" if cdp.ok else "not_running",
                        "workspace_set": False, "model_set": False,
                        "note": "工作区/模型由任务 deeplink 在执行时绑定",
                        "error": "" if cdp.ok else cdp.reason,
                        "evidence": cdp.evidence,
                    })
                else:
                    command = cfg.get("command")
                    exe = str(cfg.get("executable") or (
                        command[0] if isinstance(command, list) and command else ""))
                    usable = bool(exe) and (Path(exe).exists() or shutil.which(exe))
                    item.update({
                        "ok": usable and is_auto_executable(agent),
                        "stage": "ready" if usable else "executable",
                        "process": "cli", "workspace_set": True, "model_set": True,
                        "error": "" if usable else f"可执行文件不可用: {exe or '(未登记)'}",
                    })
            except Exception as exc:
                item.update({"ok": False, "stage": "error",
                             "error": f"{type(exc).__name__}: {exc}"})
            report.append(item)
        return {"workspace": root, "opened": bool(open_apps),
                "ready": sum(1 for r in report if r.get("ok")),
                "total": len(report), "agents": report}

    # ------------------------------------------------------------------
    # Model recommendation: cheapest sufficient, best value
    # ------------------------------------------------------------------
    def recommend_models(self, *, task_type: str = "analysis",
                         limit: int = 8,
                         profile: str = "balanced") -> dict[str, Any]:
        """Rank (agent, model) pairs by quality first, then by real cost."""
        from .control import trae_interface

        required = set(TASK_CAPABILITY_REQUIREMENTS.get(task_type, set()))
        catalog = self.model_catalog.catalog()
        bench = {row["agent_id"]: row for row in
                 self.cost.benchmark(persist=False)["agents"]}
        trae_rates: dict[str, dict[str, float]] = {}
        for app in ("trae_cn", "trae"):
            try:
                trae_rates[app] = {row["id"]: row["rate"] for row in
                                   trae_interface.list_trae_models(app, None)
                                   if row.get("rate") is not None}
            except Exception:
                trae_rates[app] = {}
        candidates: list[dict[str, Any]] = []
        for agent in self.list_agents():
            caps = set(derive_capability_list(agent))
            if required - caps or agent.status == "offline":
                continue
            rows = catalog["by_agent"].get(agent.agent_id, {}).get("models", [])
            if not rows:
                continue
            record = bench.get(agent.agent_id, {})
            quality = float(record.get("score") or 0.6)
            verification = float(record.get("verification_pass_rate") or 0.5)
            duration = float(record.get("avg_duration_ms") or 0)
            speed = 1.0 - min(1.0, duration / max(1, self.config.router_slow_reference_ms)) \
                if duration else 0.5
            for row in rows:
                name = str(row["name"])
                rate = trae_rates.get(str(agent.adapter_config.get("gui_app") or ""), {}).get(name)
                if "free" in name.lower():
                    cost, source = 0.0, "free_model"
                elif rate is not None:
                    cost, source = float(rate), "trae_catalog_rate"
                else:
                    cost, source = 0.5, "estimated"
                level = str(row.get("capability_level") or "")
                level_bias = CAPABILITY_RANK.get(
                    normalize_capability_level(level), 0.0)
                candidates.append({
                    "agent_id": agent.agent_id, "agent_name": agent.name,
                    "model": name, "adapter": agent.adapter_type,
                    "quality": round(max(0.0, min(1.0, quality + level_bias)), 4),
                    "verification": round(verification, 4),
                    "speed": round(speed, 4),
                    "cost": round(cost, 4), "cost_source": source,
                    "context_length": int(row.get("context_length") or 0),
                    "capability_level": level,
                    "capabilities": sorted(required & caps),
                })
        if not candidates:
            return {"task_type": task_type, "recommended": None,
                    "candidates": [], "reason": "没有满足能力要求的 Agent/模型"}
        max_cost = max(c["cost"] for c in candidates) or 1.0
        for c in candidates:
            c["cost_score"] = round(1.0 - c["cost"] / max_cost, 4)
            c["value"] = round(0.55 * c["quality"] + 0.25 * c["verification"]
                               + 0.20 * c["speed"], 4)
        best_quality = max(c["quality"] for c in candidates)
        profile = profile if profile in ("cost_first", "balanced", "quality_first") \
            else "balanced"
        ratio = {"cost_first": 0.8, "balanced": 0.9, "quality_first": 1.0}[profile]
        # Never floor above the best available quality: with no history every
        # agent sits at the 0.5 prior and a 0.55 floor would empty the set.
        threshold = best_quality * ratio
        sufficient = [c for c in candidates if c["quality"] >= threshold] or candidates
        cheapest = min(sufficient, key=lambda c: (c["cost"], -c["value"]))
        best = max(candidates, key=lambda c: (c["value"], -c["cost"]))
        picked = best if profile == "quality_first" else cheapest
        picked["rationale"] = (
            f"[{profile}] 质量 {picked['quality']:.2f} ≥ 阈值 {threshold:.2f}；"
            f"成本 {picked['cost']}（{picked['cost_source']}）"
            + ("；取综合价值最高" if profile == "quality_first" else "；取满足质量的最低价"))
        best["rationale"] = (
            f"综合价值 {best['value']:.2f} 最高（质量 {best['quality']:.2f}，"
            f"成本 {best['cost']}）")
        ranked = sorted(candidates, key=lambda c: (-c["value"], c["cost"]))[:limit]
        return {"task_type": task_type, "required": sorted(required),
                "profile": profile, "recommended": picked, "best_value": best,
                "threshold": round(threshold, 4),
                "note": ("质量取该 Agent 的历史 benchmark（无历史用 0.6 先验）；"
                         "成本优先取应用自身目录倍率，free 模型为 0，其余为估计值"),
                "candidates": ranked}

    # ------------------------------------------------------------------
    # Per-run binding report (workspace + model as actually applied)
    # ------------------------------------------------------------------
    def run_bindings(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise NotFoundError(f"运行不存在: {run_id}")
        rows: list[dict[str, Any]] = []
        for task in sorted(self.store.list_tasks(run_id),
                           key=lambda t: int(re.sub(r"\D", "", t.task_id) or 0)):
            agent = self.store.get_agent(task.assigned_agent) if task.assigned_agent else None
            gui_log = Path(task.task_dir or "") / "gui" / "gui.log"
            verify: dict[str, Any] = {}
            visible = ""
            if gui_log.exists():
                for line in gui_log.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event") == "interface_verify":
                        verify = event
                    if event.get("event") == "trae_visible_model":
                        visible = str(event.get("visible") or "")
            rows.append({
                "task_id": task.task_id, "agent_id": task.assigned_agent,
                "agent_name": agent.name if agent else "",
                "role": task.spec.role,
                "model": task.spec.model or str(
                    ((agent.extra or {}) if agent else {}).get("selected_model") or ""),
                "workspace": task.workdir,
                "status": task.status, "verification": task.verification_status,
                "visible_model": visible,
                "verified": bool(verify.get("ok")),
                "verify_method": verify.get("method", ""),
                "error": task.error,
            })
        return {"run_id": run_id, "status": run["status"], "stages": rows}

    # ------------------------------------------------------------------
    # Chain draft persistence (survives WebUI refreshes and restarts)
    # ------------------------------------------------------------------
    def chain_draft(self) -> dict[str, Any]:
        path = Path(self.paths.base) / "chain-draft.json"
        data = read_json(path, {}) or {}
        if not isinstance(data, dict):
            return {}
        data["path"] = str(path)
        return data

    def save_chain_draft(self, draft: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(draft, dict):
            raise ValidationError("链路草稿必须是 JSON 对象")
        stages = draft.get("stages")
        if stages is not None and not isinstance(stages, list):
            raise ValidationError("stages 必须是数组")
        path = Path(self.paths.base) / "chain-draft.json"
        existing = read_json(path, {}) or {}
        existing_revision = int(existing.get("revision") or 0)
        base_revision = draft.get("base_revision")
        if base_revision is not None and str(base_revision) != "":
            try:
                base_value = int(base_revision)
            except (TypeError, ValueError):
                raise ValidationError("base_revision 必须是整数")
            if existing_revision and base_value != existing_revision:
                raise ConflictError(
                    "草稿已被另一处更新，请刷新后合并",
                    details={"server_revision": existing_revision,
                             "base_revision": base_value},
                )
        revision = existing_revision + 1
        payload = {
            "project_id": self.project.project_id,
            "revision": revision,
            "updated_at": now_iso(),
            "updated_by": str(draft.get("client_id") or ""),
            "plan_id": str(draft.get("plan_id") or ""),
            "goal": str(draft.get("goal") or ""),
            "policy": str(draft.get("policy") or "balanced"),
            "main_agent_id": str(draft.get("main_agent_id") or ""),
            "stages": [
                {
                    "task_id": str(item.get("task_id") or ""),
                    "title": str(item.get("title") or ""),
                    "agent_id": str(item.get("agent_id") or ""),
                    "model": str(item.get("model") or ""),
                    "type": str(item.get("type") or "analysis"),
                }
                for item in (stages or [])
                if isinstance(item, dict)
            ],
        }
        write_json(path, payload)
        self.audit.log("user", "chain.draft_saved", payload={
            "plan_id": payload["plan_id"], "stages": len(payload["stages"]),
        })
        return {**payload, "path": str(path)}

    # ------------------------------------------------------------------
    # Manual intervention / blocked nodes
    # ------------------------------------------------------------------
    def manual_actions(self, run_id: str = "") -> list[dict[str, Any]]:
        run = self.store.get_run(run_id) if run_id else self.latest_run()
        if not run:
            return []
        out: list[dict[str, Any]] = []
        for task in self.store.list_tasks(run["run_id"]):
            if task.status != TaskStatus.WAITING_USER.value:
                continue
            payload: dict[str, Any] = {}
            if task.task_dir:
                payload = read_json(Path(task.task_dir) / "manual-action.json", {}) or {}
            if not payload:
                # Budget/high-risk approval waits have their own section-4
                # queue; only adapter-reported human actions pop the alert.
                continue
            agent = self.store.get_agent(task.assigned_agent) if task.assigned_agent else None
            out.append({
                "task_uid": task.uid, "task_id": task.task_id,
                "title": task.spec.title, "status": task.status,
                "agent_id": task.assigned_agent,
                "agent_name": agent.name if agent else (payload.get("agent_name") or ""),
                "model": task.spec.model,
                "error": task.error,
                "manual_action": payload if isinstance(payload, dict) else {},
            })
        seen = {item["task_uid"] for item in out}
        run_dir = Path(self.paths.run_dir(run["run_id"]))
        round_number = int(run.get("round_number") or 1)

        def add_synthetic(path: Path, task_id: str, title: str) -> None:
            if not path.exists():
                return
            data = read_json(path, {})
            if not isinstance(data, dict) or not data:
                return
            uid = f"{run['run_id']}:{task_id}"
            if uid in seen:
                return
            agent_id = str(data.get("agent_id") or run.get("main_agent_id") or "")
            agent = self.store.get_agent(agent_id) if agent_id else None
            out.append({
                "task_uid": uid, "task_id": task_id, "title": title,
                "status": TaskStatus.WAITING_USER.value,
                "agent_id": agent_id,
                "agent_name": agent.name if agent else (data.get("agent_name") or ""),
                "model": str(data.get("model") or run.get("model") or ""),
                "error": f"MANUAL_ACTION_REQUIRED: {data.get('message', '')}",
                "manual_action": data,
                "synthetic": True,
            })
            seen.add(uid)

        # Synthetic main-node blocks belong to a live run; a paused/finished
        # run must not keep showing an old summary/planning prompt.
        if run.get("status") in ("running", "waiting_user"):
            add_synthetic(run_dir / "summary" / "manual-action.json",
                          "SUMMARY", "主节点汇总")
            for path in Path(self.paths.planning).glob(
                    f"round{round_number}_*/manual-action.json"):
                add_synthetic(path, f"ROUND{round_number}_PLAN",
                              f"第 {round_number} 轮主节点规划")
        return out

    def main_activity(self, run_id: str = "") -> dict[str, Any]:
        """Latest visible main-node activity (summary / next-round planning)."""
        run = self.store.get_run(run_id) if run_id else self.latest_run()
        if not run:
            return {}
        if run.get("status") not in ("running", "waiting_user", "ready"):
            return {}
        candidates: list[tuple[float, dict[str, Any]]] = []
        summary_path = Path(self.paths.run_dir(run["run_id"])) / "summary" / "activity.json"
        for path in (summary_path,):
            data = read_json(path, {}) if path.exists() else {}
            if isinstance(data, dict) and data:
                candidates.append((path.stat().st_mtime, {**data, "path": str(path)}))
        round_number = int(run.get("round_number") or 1)
        for pattern in (f"round{round_number}_*/activity.json", "*/activity.json"):
            found = False
            for path in Path(self.paths.planning).glob(pattern):
                if not path.is_file():
                    continue
                data = read_json(path, {})
                # Old completed planning records are history, not "current
                # main-node activity"; only show a live/waiting plan.
                if isinstance(data, dict) and data and \
                        data.get("status") in ("running", "waiting_user", "waiting_agent"):
                    candidates.append((path.stat().st_mtime, {**data, "path": str(path)}))
                    found = True
            if found:
                break
        if not candidates:
            return {}
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def restart_agent_interface(self, agent_id: str) -> dict[str, Any]:
        """Explicit user action: restart one desktop app with its CDP interface.

        Never called automatically.  The WebUI shows the button only when an
        app is running without the debugging interface.
        """
        from .control import trae_interface
        from .control.interfaces import ensure_workbuddy_cdp

        agent = self.get_agent(agent_id)
        cfg = dict(agent.adapter_config or {})
        app = str(cfg.get("gui_app") or "")
        if app in ("trae_cn", "trae"):
            outcome = trae_interface.ensure_trae_cdp(
                app=app,
                port=int(cfg.get("trae_cdp_port") or trae_interface.CDP_DEFAULT_PORT),
                executable=str(cfg.get("executable") or ""),
                config=cfg, allow_restart=True, force_restart=True,
                workspace=str(self.project.root),
                before_launch=lambda: trae_interface.set_trae_workspace(
                    workspace=str(self.project.root), app=app, config=cfg),
            )
        elif app == "workbuddy":
            outcome = ensure_workbuddy_cdp(
                executable=str(cfg.get("executable") or ""),
                port=int(cfg.get("workbuddy_cdp_port") or 9223),
                allow_restart=True, force_restart=True,
            )
        else:
            raise ValidationError(f"{agent_id} 没有可重启的桌面程序接口")
        self.audit.log("user", "agent.interface_restart", subject=agent_id,
                       payload={"app": app, "ok": outcome.ok,
                                "reason": outcome.reason,
                                "method": outcome.method,
                                "evidence": outcome.evidence})
        return {"agent_id": agent_id, "app": app, **outcome.to_dict()}

    def chain_status(self, run_id: str = "") -> dict[str, Any]:
        run = self.store.get_run(run_id) if run_id else self.latest_run()
        if not run:
            return {"status": "no_run", "stages": []}
        tasks = sorted(self.store.list_tasks(run["run_id"]),
                       key=lambda t: int(re.sub(r"\D", "", t.task_id) or 0))
        stages = []
        for task in tasks:
            agent_id = task.assigned_agent or task.spec.suggested_agent
            agent = self.store.get_agent(agent_id) if agent_id else None
            stages.append({
                "task_id": task.task_id, "status": task.status,
                "agent_id": agent_id, "agent_name": agent.name if agent else "",
                "role": task.spec.role,
                "model": task.spec.model or str(
                    ((agent.extra or {}) if agent else {}).get("selected_model") or ""),
                "verification": task.verification_status, "error": task.error,
                "objective": task.spec.objective,
            })
        plan = self.store.get_plan(run["plan_id"])
        return {"run_id": run["run_id"], "status": run["status"],
                "acceptance_status": run.get("acceptance_status", ""),
                "release_status": run.get("release_status", ""),
                "goal": plan.goal if plan else "", "stages": stages}

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
            "manual_actions": self.manual_actions(run["run_id"] if run else ""),
            "main_activity": self.main_activity(run["run_id"] if run else ""),
            "chain_draft": self.chain_draft(),
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
            "modules": self._module_status(),
        }

    def _module_status(self) -> dict[str, str]:
        """Module health is evidence-based, never a hardcoded all-ready list."""
        agents = self.list_agents()
        online = [a for a in agents if a.status not in ("offline", "error", "unknown")]
        try:
            bus_ok = bool(self.bus.verify().get("ok"))
        except Exception:
            bus_ok = False
        return {
            "connector": "ready" if online else ("degraded" if agents else "unavailable"),
            "unified_memory": "ready",
            "context_bus": "ready" if bus_ok else "degraded",
            "task_scheduler": "ready" if agents else "unavailable",
            "capability_router": "ready" if online else "degraded",
            # Without a price table USD cost is unknown, not proven zero.
            "cost_optimizer": "ready" if self.config.pricing else "unverified",
            "human_governance": "ready",
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

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(10.0):
            try:
                self.store.heartbeat_controller_instance(self.instance_id)
            except Exception:
                continue

    def _startup_reconcile(self) -> None:
        """Resume verification/queued work from a crashed controller.

        Only runs that were still `running` are touched; explicit pauses and
        waiting-human states are never auto-started.
        """
        try:
            time.sleep(0.5)
            self.store.drain_outbox()
            live = self.store.live_controller_ids(
                self.project.project_id, ttl_seconds=30.0, exclude=self.instance_id)
            for run in self.store.list_runs(self.project.project_id, limit=20):
                if run.get("status") != "running":
                    continue
                run_owner = str(run.get("owner_id") or "")
                if run_owner and run_owner in live:
                    continue
                tasks = self.store.list_tasks(run["run_id"])
                if any(task.status in (TaskStatus.VERIFYING.value,
                                       TaskStatus.RUNNING.value,
                                       TaskStatus.QUEUED.value) for task in tasks):
                    self.runner.schedule_reconcile(
                        run["run_id"], "startup_recovery")
        except Exception:
            return

    def close(self) -> None:
        try:
            self._heartbeat_stop.set()
        except Exception:
            pass
        try:
            self.store.deactivate_controller_instance(self.instance_id)
        except Exception:
            pass
        self.db.close_thread()


def _approval_id_for(hub: "Hub", kind: str, plan_id: str) -> str:
    for item in hub.approvals_all(limit=500):
        if item["kind"] == kind and item.get("plan_id") == plan_id \
                and item["status"] in ("pending", "approved"):
            return str(item["approval_id"])
    return ""


def _count_by(items: list[Any], key) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        k = key(item)
        out[k] = out.get(k, 0) + 1
    return out
