"""Model-level health from real provider errors (subscription/auth/quota).

2026-11-07 real-chain defect #3: every ZCode provider subscription had expired
(InvalidSubscription) yet the catalog kept offering the models and the planner
kept binding them; five retries burned on the same dead provider.  Health is
recorded only from observed evidence — a failed execution's provider error or
a minimal headless probe — never guessed.  A degraded model blocks new pins
and is skipped by the role ladder/recommender until a probe or a successful
execution proves it works again.
"""
from __future__ import annotations

from typing import Any

from ..models import AgentRecord
from ..util import now_iso


def classify_model_error(error: str) -> str:
    """Classify a provider error text; empty string means "not a health signal"."""
    text = str(error or "")
    low = text.lower()
    if not low:
        return ""
    if "invalidsubscription" in low or "订阅" in text or (
            "subscription" in low and any(
                token in low for token in ("invalid", "expired", "失效", "not active"))):
        return "subscription_invalid"
    if any(token in low for token in (
            "invalidapikey", "invalid api key", "invalid-api-key",
            "unauthorized", "authentication", "apikeyerror", "401",
            "missing_credential", "no api key")):
        return "auth_invalid"
    if any(token in low for token in (
            "quota", "insufficient", "额度", "余额", "arrearage", "overdue")):
        return "quota_exhausted"
    return ""


def health_map(agent: AgentRecord) -> dict[str, dict[str, Any]]:
    extra = agent.extra or {}
    health = extra.get("model_health")
    return dict(health) if isinstance(health, dict) else {}


def model_health(agent: AgentRecord, model: str) -> dict[str, Any]:
    entry = health_map(agent).get(str(model or ""))
    return dict(entry) if isinstance(entry, dict) else {}


def is_degraded(agent: AgentRecord, model: str) -> bool:
    return str(model_health(agent, model).get("status") or "") == "degraded"


def degraded_entry(kind: str, error: str, *, source: str) -> dict[str, Any]:
    return {"status": "degraded", "reason": kind, "error": str(error or "")[:300],
            "checked_at": now_iso(), "source": source}


def ok_entry(*, source: str) -> dict[str, Any]:
    return {"status": "ok", "error": "", "checked_at": now_iso(), "source": source}


HEALTH_LABELS = {
    "subscription_invalid": "订阅失效",
    "auth_invalid": "鉴权失败",
    "quota_exhausted": "额度不足",
}


__all__ = ["classify_model_error", "health_map", "model_health", "is_degraded",
           "degraded_entry", "ok_entry", "HEALTH_LABELS"]