"""Agent model catalog: the real model list each agent can run.

需求：获取所有 Agent 中支持的模型列表，并在选择主/子 Agent 时选配合适的模型。
实现原则：

* 只返回本机真实可观测的模型：CLI 输出、Agent 自身会话事件、Agent 自己的
  provider/settings 配置，或用户在控制台显式登记的模型；
* 不内置任何“猜测”的模型目录，也不读取/暴露密钥；
* 选择结果持久化到 ``agents.extra.selected_model``，执行器优先使用它。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from ..adapters.discovery import DiscoveryContext, detect_models_for_agent
from ..config import HubConfig
from .interfaces import list_qoder_models, list_workbuddy_models
from ..errors import NotFoundError, ValidationError
from ..models import AgentRecord, Project
from ..util import now_iso
from ..storage.repo import Store


SOURCE_LABELS: dict[str, str] = {
    "discovered": "本地探测",
    "session": "会话事件",
    "cli": "CLI 输出",
    "registered": "Agent 登记",
    "user": "用户登记",
}


def _display_name(agent_id: str, model: str) -> str:
    """Human name exactly as the agent's own UI renders it."""
    name = str(model or "")
    if agent_id in ("trae", "trae_cn"):
        try:
            from . import trae_interface
            row = trae_interface.resolve_trae_model(name, app=agent_id)
            return str((row or {}).get("display_name") or name)
        except Exception:
            return name
    if agent_id in ("qoder", "qoder_cn"):
        from .interfaces import QODER_MODEL_LABELS
        return QODER_MODEL_LABELS.get(name, name)
    if agent_id == "workbuddy":
        from .interfaces import WORKBUDDY_MODEL_LABELS
        row = _workbuddy_map().get(name)
        if row:
            return str(row.get("name") or name)
        if name in WORKBUDDY_MODEL_LABELS:
            return name  # already the human label
        return WORKBUDDY_MODEL_LABELS.get(name, name)
    return name


@lru_cache(maxsize=1)
def _workbuddy_map() -> dict[str, dict[str, Any]]:
    from .interfaces import list_workbuddy_models
    try:
        return {row["id"]: row for row in list_workbuddy_models()}
    except Exception:
        return {}


def _model_rate(agent_id: str, model: str) -> float | None:
    if agent_id == "workbuddy":
        from .interfaces import WORKBUDDY_MODEL_LABELS
        mapping = _workbuddy_map()
        row = mapping.get(str(model))
        if not row and str(model) in WORKBUDDY_MODEL_LABELS:
            row = mapping.get(WORKBUDDY_MODEL_LABELS[str(model)])
        return (row or {}).get("rate")
    if agent_id not in ("trae", "trae_cn"):
        return None
    try:
        from . import trae_interface
        row = trae_interface.resolve_trae_model(model, app=agent_id)
        return (row or {}).get("rate")
    except Exception:
        return None


def _dedupe_by_display(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per human model name (ids and labels can both be registered)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in models:
        key = str(item.get("display_name") or item.get("name") or "").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


class ModelCatalogService:
    def __init__(self, store: Store, project: Project, config: HubConfig, *,
                 discovery_ctx: DiscoveryContext | None = None):
        self.store = store
        self.project = project
        self.config = config
        self.discovery_ctx = discovery_ctx

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def catalog(self, *, refresh: bool = False) -> dict[str, Any]:
        if refresh:
            self.refresh()
        agents = self.store.list_agents()
        rows = self.store.list_agent_models(self.project.project_id)
        by_agent: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_agent.setdefault(row["agent_id"], []).append(row)
        main = self.store.get_main_node(self.project.project_id)
        main_id = (main or {}).get("agent_id", "")
        out_agents: list[dict[str, Any]] = []
        flat: list[dict[str, Any]] = []
        for agent in agents:
            entries = by_agent.get(agent.agent_id, [])
            if not entries and agent.models:
                entries = [
                    {"model": name, "source": "registered", "note": "Agent 登记模型"}
                    for name in agent.models
                ]
            extra = agent.extra or {}
            selected = str(extra.get("selected_model") or "")
            if not selected and main_id == agent.agent_id and main:
                selected = str(main.get("model") or "")
            models = [
                {
                    "name": entry["model"],
                    "display_name": _display_name(agent.agent_id, entry["model"]),
                    "rate": _model_rate(agent.agent_id, entry["model"]),
                    "source": entry["source"],
                    "source_label": SOURCE_LABELS.get(entry["source"], entry["source"]),
                    "note": entry.get("note", ""),
                }
                for entry in sorted(entries, key=lambda e: (e.get("source", ""), e["model"]))
            ]
            models = _dedupe_by_display(models)
            item = {
                "agent_id": agent.agent_id, "name": agent.name, "product": agent.product,
                "status": agent.status, "version": agent.version,
                "adapter_type": agent.adapter_type, "access_level": agent.access_level,
                "selected_model": selected, "models": models,
                "is_main": main_id == agent.agent_id,
            }
            out_agents.append(item)
            for model in models:
                flat.append({
                    "agent_id": agent.agent_id, "agent_name": agent.name,
                    "name": model["name"], "source": model["source"],
                    "source_label": model["source_label"],
                })
        return {
            "agents": out_agents,
            "by_agent": {item["agent_id"]: item for item in out_agents},
            "models": flat,
            "sources": SOURCE_LABELS,
            "main": {"agent_id": main_id, "model": (main or {}).get("model", "")},
        }

    def agent_models(self, agent_id: str) -> dict[str, Any]:
        agent = self._agent(agent_id)
        catalog = self.catalog()
        return catalog["by_agent"].get(agent.agent_id, {
            "agent_id": agent.agent_id, "name": agent.name,
            "selected_model": "", "models": [],
        })

    # ------------------------------------------------------------------
    # Refresh / register / select
    # ------------------------------------------------------------------
    def refresh(self, *, agent_id: str = "", source: str = "discovered") -> dict[str, Any]:
        agents = [self._agent(agent_id)] if agent_id else self.store.list_agents()
        refreshed: list[dict[str, Any]] = []
        for agent in agents:
            detected = list(detect_models_for_agent(agent, self.discovery_ctx))
            if agent.agent_id == "workbuddy":
                detected.extend([row["id"] for row in list_workbuddy_models()])
            elif agent.agent_id == "qoder_cn":
                detected.extend([row["id"] for row in list_qoder_models()])
            detected = sorted({str(item) for item in detected if str(item).strip()})
            if detected:
                self.store.replace_discovered_models(
                    self.project.project_id, agent.agent_id, detected,
                    source=source,
                )
            rows = self.store.list_agent_models(self.project.project_id, agent_id=agent.agent_id)
            models = sorted({row["model"] for row in rows} | set(agent.models or []))
            if models != sorted(set(agent.models or [])):
                self.store.set_agent_models(agent.agent_id, models)
            refreshed.append({
                "agent_id": agent.agent_id,
                "detected": detected,
                "total": len(models),
            })
        return {"status": "refreshed", "agents": refreshed,
                "catalog": self.catalog()}

    def sync_all(self, *, agent_id: str = "") -> dict[str, Any]:
        """Refresh every agent's real catalog and report per-agent coverage."""
        result = self.refresh(agent_id=agent_id, source="sync")
        return {
            "status": "synced",
            "synced_at": now_iso(),
            "agents": [
                {"agent_id": row["agent_id"], "detected": len(row.get("detected") or []),
                 "total": row.get("total", 0)}
                for row in result.get("agents", [])
            ],
            "catalog": result.get("catalog"),
        }

    def register(self, agent_id: str, models: list[str], *, source: str = "user",
                 note: str = "") -> dict[str, Any]:
        if source not in ("user", "registered"):
            raise ValidationError("只能登记 user/registered 来源的模型")
        agent = self._agent(agent_id)
        added: list[dict[str, Any]] = []
        for model in models:
            if not str(model or "").strip():
                continue
            added.append(self.store.upsert_agent_model(
                self.project.project_id, agent.agent_id, str(model).strip(),
                source=source, note=note,
            ))
        self._sync_agent_models(agent)
        return {"agent_id": agent.agent_id, "added": added,
                "models": self.agent_models(agent.agent_id)}

    def select(self, agent_id: str, model: str) -> dict[str, Any]:
        agent = self._agent(agent_id)
        name = str(model or "").strip()
        if name:
            known = {row["model"] for row in self.store.list_agent_models(
                self.project.project_id, agent_id=agent.agent_id)}
            if name not in known:
                self.store.upsert_agent_model(
                    self.project.project_id, agent.agent_id, name,
                    source="user", note="控制台选配模型",
                )
        extra = dict(agent.extra or {})
        extra["selected_model"] = name
        self.store.update_agent_extra(agent.agent_id, extra)
        self._sync_agent_models(agent)
        return {
            "agent_id": agent.agent_id, "selected_model": name,
            "models": self.agent_models(agent.agent_id),
        }

    def remove(self, agent_id: str, model: str) -> dict[str, Any]:
        agent = self._agent(agent_id)
        self.store.delete_agent_model(self.project.project_id, agent.agent_id, model)
        extra = dict(agent.extra or {})
        if extra.get("selected_model") == model:
            extra["selected_model"] = ""
            self.store.update_agent_extra(agent.agent_id, extra)
        self._sync_agent_models(agent)
        return self.agent_models(agent.agent_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _sync_agent_models(self, agent: AgentRecord) -> None:
        rows = self.store.list_agent_models(self.project.project_id, agent_id=agent.agent_id)
        models = sorted({row["model"] for row in rows} | set(agent.models or []))
        self.store.set_agent_models(agent.agent_id, models)

    def _agent(self, agent_id: str) -> AgentRecord:
        agent = self.store.get_agent(str(agent_id or ""))
        if not agent:
            raise NotFoundError(f"Agent 不存在: {agent_id}")
        return agent


__all__ = ["ModelCatalogService", "SOURCE_LABELS"]
