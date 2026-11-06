"""Reverse-engineered model profile metadata per local Agent.

Every GUI agent keeps its own model settings:

* WorkBuddy product manifest -> ``contextWindow.supportedLengths`` and
  ``reasoning.supportedEfforts``;
* Trae ``state.vscdb`` model catalog -> ``context_window_size`` and
  ``reasoning_effort_config.options``;
* Qoder ``chat_model_preferences`` -> current ``context_window`` and
  ``reasoning_effort`` (its effort list is the app's fixed option set).

The control plane must never invent values: selection, task binding and the
trigger-time sync all resolve against these lists.
"""

from __future__ import annotations

from typing import Any

from ..errors import ValidationError
from ..models import (CAPABILITY_LEVELS, AgentRecord,
                      normalize_capability_level)
from .interfaces import list_qoder_models, list_workbuddy_models


CAPABILITY_LABELS_BY_AGENT: dict[str, dict[str, str]] = {
    "workbuddy": {"low": "低", "high": "高", "max": "极高"},
    "trae_cn": {"light": "轻", "high": "高", "extra_high": "极高"},
    "trae": {"light": "轻", "high": "高", "extra_high": "极高"},
    "qoder_cn": {"none": "关闭思考", "minimal": "最小", "low": "低",
                 "medium": "中", "high": "高", "xhigh": "极高", "max": "最大"},
}


def agent_app(agent: AgentRecord) -> str:
    return str((agent.adapter_config or {}).get("gui_app") or agent.agent_id)


def read_source_models(agent: AgentRecord,
                       config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    app = agent_app(agent)
    try:
        if app == "workbuddy":
            return list_workbuddy_models(config)
        if app in ("trae", "trae_cn"):
            from . import trae_interface
            return trae_interface.list_trae_models(app, config)
        if app == "qoder_cn":
            return list_qoder_models(config)
    except Exception:
        return []
    return []


def _find_model(rows: list[dict[str, Any]], model: str) -> dict[str, Any]:
    key = str(model or "").strip().lower()
    if not key:
        return {}
    for row in rows:
        for field in ("id", "name", "display_name"):
            if str(row.get(field) or "").strip().lower() == key:
                return row
    return {}


def source_meta_from_rows(rows: list[dict[str, Any]], model: str,
                          app: str = "") -> dict[str, Any]:
    row = _find_model(rows, model)
    efforts = [str(value) for value in (row.get("capability_options") or [])
               if str(value or "").strip()]
    contexts = [int(value) for value in (row.get("context_options") or [])
                if isinstance(value, (int, float)) and int(value) > 0]
    labels = CAPABILITY_LABELS_BY_AGENT.get(app, {})
    return {
        "model_found": bool(row),
        "context_default": int(row.get("current_context_length")
                               or row.get("context_default") or 0),
        "context_options": sorted(set(contexts)),
        "context_max": int(row.get("context_max") or max(contexts or [0])),
        "capability_options": efforts,
        "capability_default": str(row.get("current_capability_level")
                                  or row.get("capability_default") or ""),
        "capability_labels": dict(labels),
        "catalog_source": str(row.get("source") or ""),
    }


def source_meta(agent: AgentRecord, model: str,
                config: dict[str, Any] | None = None) -> dict[str, Any]:
    app = agent_app(agent)
    return source_meta_from_rows(read_source_models(agent, config), model, app)


def _fmt(values: list[Any]) -> str:
    return ", ".join(str(value) for value in values)


def resolve_profile(agent: AgentRecord, model: str,
                    config: dict[str, Any] | None = None, *,
                    context_length: int = 0,
                    capability_level: str = "") -> dict[str, Any]:
    """Validate one Agent/model profile against the app's real option lists."""
    meta = source_meta(agent, model, config)
    label = f"{agent.agent_id}/{model or '(默认模型)'}"
    raw_level = str(capability_level or "").strip()
    level = normalize_capability_level(
        raw_level, meta.get("capability_labels") or {})
    options = list(meta.get("capability_options") or [])
    if level and not options and level not in CAPABILITY_LEVELS:
        raise ValidationError(
            f"{label} 没有公开能力等级选项，未知值 {raw_level}",
            details={"known": list(CAPABILITY_LEVELS)},
        )
    if level and options and level not in options:
        raise ValidationError(
            f"{label} 支持的能力等级只有 {_fmt(options)}，收到 {raw_level}",
            details={"capability_options": options,
                     "capability_labels": meta.get("capability_labels")},
        )
    context = max(0, int(context_length or 0))
    context_options = list(meta.get("context_options") or [])
    context_max = int(meta.get("context_max") or 0)
    if context and context_options and context not in context_options:
        raise ValidationError(
            f"{label} 支持的上下文长度只有 {_fmt(context_options)}，收到 {context}",
            details={"context_options": context_options,
                     "context_max": context_max},
        )
    if context and context_max and context > context_max:
        raise ValidationError(
            f"{label} 的上下文长度上限是 {context_max}，收到 {context}",
            details={"context_options": context_options,
                     "context_max": context_max},
        )
    return {"context_length": context, "capability_level": level}


__all__ = [
    "CAPABILITY_LABELS_BY_AGENT", "agent_app", "read_source_models",
    "source_meta", "source_meta_from_rows", "resolve_profile",
]
