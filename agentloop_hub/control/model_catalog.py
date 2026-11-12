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
from .model_profile import (agent_app, read_source_models, resolve_profile,
                            source_meta_from_rows)
from ..errors import NotFoundError, ValidationError
from ..models import AgentRecord, Project, normalize_capability_level
from ..util import now_iso
from ..storage.repo import Store


SOURCE_LABELS: dict[str, str] = {
    "discovered": "本地探测",
    "session": "会话事件",
    "cli": "CLI 输出",
    "registered": "Agent 登记",
    "user": "用户登记",
}


def _profile_warning(saved: dict[str, Any], source: dict[str, Any]) -> str:
    notes: list[str] = []
    context = int(saved.get("context_length") or 0)
    options = [int(value) for value in (source.get("context_options") or [])]
    if context and options and context not in options:
        notes.append(f"上下文 {context} 不在真实选项 {options}")
    raw_level = str(saved.get("capability_level") or "")
    level = normalize_capability_level(
        raw_level, source.get("capability_labels") or {})
    efforts = [str(value) for value in (source.get("capability_options") or [])]
    if level and efforts and level not in efforts:
        notes.append(f"能力等级 {raw_level} 不在真实选项 {efforts}")
    return "；".join(notes)


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
        from .interfaces import qoder_model_labels
        return qoder_model_labels().get(name, name)
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


def _dedupe_by_model_name(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per real model id.

    Human labels are presentation-only.  Two distinct ids can render the same
    label (``deepseek//deepseek-v4-pro`` vs ``deepseek-v4-pro-0813``) and the
    agent accepts them as different choices, so collapsing by display name
    hid real models from the picker.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in models:
        # Model ids are case-sensitive in some products (e.g. two real ids that
        # differ only by case).  Do not fold them together.
        key = str(item.get("name") or "").strip()
        if not key or key in seen:
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
        self._source_cache: dict[str, list[dict[str, Any]]] = {}

    def _source_meta(self, agent: AgentRecord, model: str) -> dict[str, Any]:
        """Real context/effort options read from the agent's own catalog."""
        app = agent_app(agent)
        if app not in self._source_cache:
            self._source_cache[app] = read_source_models(
                agent, self.config.to_dict())
        return source_meta_from_rows(self._source_cache[app], model, app)

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
            if entries and agent.agent_id in ("qoder", "qoder_cn"):
                keep = set(self._drop_qoder_aliases([e["model"] for e in entries]))
                entries = [entry for entry in entries if entry["model"] in keep]
            extra = agent.extra or {}
            profiles = extra.get("model_profiles") if isinstance(
                extra.get("model_profiles"), dict) else {}
            model_health = extra.get("model_health") if isinstance(
                extra.get("model_health"), dict) else {}
            selected = str(extra.get("selected_model") or "")
            if not selected and main_id == agent.agent_id and main:
                selected = str(main.get("model") or "")
            selected_profile = profiles.get(selected) if isinstance(
                profiles.get(selected), dict) else {}
            selected_source = self._source_meta(agent, selected) if selected else {}
            models = []
            for entry in sorted(entries, key=lambda e: (e.get("source", ""), e["model"])):
                source = self._source_meta(agent, entry["model"])
                saved = profiles.get(entry["model"]) if isinstance(
                    profiles.get(entry["model"]), dict) else {}
                models.append({
                    "name": entry["model"],
                    "display_name": _display_name(agent.agent_id, entry["model"]),
                    "rate": _model_rate(agent.agent_id, entry["model"]),
                    "source": entry["source"],
                    "source_label": SOURCE_LABELS.get(entry["source"], entry["source"]),
                    "note": entry.get("note", ""),
                    "health": model_health.get(entry["model"]) or {},
                    "context_length": int(saved.get("context_length") or 0),
                    "capability_level": str(
                        saved.get("capability_level")
                        or source.get("capability_default") or ""),
                    "profile_warning": _profile_warning(saved, source),
                    **source,
                })
            models = _dedupe_by_model_name(models)
            item = {
                "agent_id": agent.agent_id, "name": agent.name, "product": agent.product,
                "status": agent.status, "version": agent.version,
                "adapter_type": agent.adapter_type, "access_level": agent.access_level,
                "selected_model": selected, "models": models,
                "context_length": int(selected_profile.get("context_length")
                                       or selected_source.get("context_default") or 0),
                "capability_level": str(selected_profile.get("capability_level")
                                        or selected_source.get("capability_default") or ""),
                "context_options": selected_source.get("context_options", []),
                "context_max": selected_source.get("context_max", 0),
                "capability_options": selected_source.get("capability_options", []),
                "capability_labels": selected_source.get("capability_labels", {}),
                "profile_warning": _profile_warning(
                    selected_profile, selected_source),
                "model_profiles": profiles,
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
                if source != "sync":
                    # Explicit refresh: read the desktop's own model menu so
                    # keys and labels match exactly.  Background sync skips
                    # this because it would pop the menu open.
                    try:
                        from ..gui.cdp import qoder_model_catalog_via_cdp
                        port = int((agent.adapter_config or {}).get(
                            "qoder_cdp_port") or 9222)
                        live = qoder_model_catalog_via_cdp(port=port, timeout=6)
                        if live.get("ok"):
                            detected.extend(item["key"] for item in
                                            (live.get("items") or []))
                    except Exception:
                        pass
            detected = sorted({str(item) for item in detected if str(item).strip()})
            if detected:
                self.store.replace_discovered_models(
                    self.project.project_id, agent.agent_id, detected,
                    source=source,
                )
            rows = self.store.list_agent_models(self.project.project_id, agent_id=agent.agent_id)
            models = sorted({row["model"] for row in rows} | set(agent.models or []))
            if agent.agent_id in ("qoder", "qoder_cn"):
                models = self._drop_qoder_aliases(models)
            if models != sorted(set(agent.models or [])):
                self.store.set_agent_models(agent.agent_id, models)
            probed = self._probe_model_health(agent, models) if source == "sync" else {}
            refreshed.append({
                "agent_id": agent.agent_id,
                "detected": detected,
                "total": len(models),
                "health_probed": probed,
            })
        return {"status": "refreshed", "agents": refreshed,
                "catalog": self.catalog()}

    def _drop_qoder_aliases(self, models: list[str]) -> list[str]:
        """Keep real model keys only: labels are presentation, not choices.

        Older runs registered display labels (``DeepSeek-Flash``) next to the
        real runtime key (``dfmodel``), and session scraping produced
        ``<synthetic>``.  Both made the picker disagree with Qoder's own menu.
        """
        from .interfaces import qoder_model_labels
        label_to_key = {label: key for key, label in qoder_model_labels().items()}
        present = set(models)
        kept: list[str] = []
        for model in models:
            if model == "<synthetic>":
                continue
            key = label_to_key.get(model)
            if key and key != model and key in present:
                continue
            kept.append(model)
        return kept

    def _probe_model_health(self, agent: AgentRecord,
                            models: list[str]) -> dict[str, dict[str, Any]]:
        """Sync-time subscription probe (currently ZCode's headless CLI).

        2026-11-07 defect #3: expired provider subscriptions were invisible to
        the catalog, so the planner kept binding dead models.  Only agents
        with a real probe channel are probed; results are evidence-backed
        (ok/degraded), never guessed.  ``adapter_config.probe_on_sync=false``
        opts out.
        """
        if agent.adapter_type != "zcode" or not models:
            return {}
        cfg = agent.adapter_config or {}
        if str(cfg.get("probe_on_sync", "true")).strip().lower() in ("false", "0", "no", "off"):
            return {}
        from ..adapters.zcode import ZcodeAdapter
        from .model_health import degraded_entry, ok_entry
        adapter = ZcodeAdapter(agent)
        timeout = float(cfg.get("probe_timeout_seconds") or 60)
        fresh = self.store.get_agent(agent.agent_id) or agent
        extra = dict(fresh.extra or {})
        health = dict(extra.get("model_health") or {})
        results: dict[str, dict[str, Any]] = {}
        for model in models:
            probe = adapter.probe_model(model, timeout=timeout)
            status = str(probe.get("status") or "")
            if status == "ok":
                health[model] = ok_entry(source="probe")
            elif status == "degraded":
                health[model] = degraded_entry(
                    str(probe.get("reason") or "probe_failed"),
                    str(probe.get("error") or ""), source="probe")
            else:
                continue  # unknown probe keeps the previous evidence
            results[model] = health[model]
        if results:
            extra["model_health"] = health
            self.store.update_agent_extra(agent.agent_id, extra)
        return results

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

    def select(self, agent_id: str, model: str, *,
               context_length: int | None = None,
               capability_level: str | None = None) -> dict[str, Any]:
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
        validated = resolve_profile(
            agent, name, self.config.to_dict(),
            context_length=int(context_length or 0),
            capability_level=str(capability_level or ""),
        ) if name else {"context_length": 0, "capability_level": ""}
        extra = dict(agent.extra or {})
        extra["selected_model"] = name
        if context_length is not None or capability_level is not None:
            profiles = dict(extra.get("model_profiles") or {})
            profile = dict(profiles.get(name) or {})
            if context_length is not None:
                profile["context_length"] = int(validated["context_length"])
            if capability_level is not None:
                profile["capability_level"] = str(
                    validated["capability_level"])
            profiles[name] = profile
            extra["model_profiles"] = profiles
            # Keep effective convenience fields for older WebUI/CLI readers.
            extra["context_length"] = int(profile.get("context_length") or 0)
            extra["capability_level"] = str(profile.get("capability_level") or "")
        self.store.update_agent_extra(agent.agent_id, extra)
        self._sync_agent_models(agent)
        return {
            "agent_id": agent.agent_id, "selected_model": name,
            "context_length": int(extra.get("context_length") or 0),
            "capability_level": str(extra.get("capability_level") or ""),
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
        if agent.agent_id in ("qoder", "qoder_cn"):
            models = self._drop_qoder_aliases(models)
        self.store.set_agent_models(agent.agent_id, models)

    def _agent(self, agent_id: str) -> AgentRecord:
        agent = self.store.get_agent(str(agent_id or ""))
        if not agent:
            raise NotFoundError(f"Agent 不存在: {agent_id}")
        return agent


__all__ = ["ModelCatalogService", "SOURCE_LABELS"]
