"""Agent software version ledger and compatibility drift detection.

每次探测 Agent 时记录版本与集成指纹。Agent 升级后如果适配器类型、能力集合或
模型集合出现退化，会被标记为 ``drift``，控制台给出兼容提示，避免“Agent 更新
后 AgentMesh OS 突然不可用”而无法定位原因。
"""

from __future__ import annotations

from typing import Any

from ..models import AgentRecord, Project
from ..storage.repo import Store


COMPAT_LABELS: dict[str, str] = {
    "ok": "兼容",
    "upgraded": "已升级（兼容）",
    "drift": "能力漂移，需要复核",
}


class AgentVersionService:
    def __init__(self, store: Store, project: Project):
        self.store = store
        self.project = project

    def record(self, agents: list[AgentRecord]) -> list[dict[str, Any]]:
        out = []
        for agent in agents:
            record = self.store.record_agent_version(self.project.project_id, agent)
            if record:
                out.append(record)
        return out

    def status(self) -> dict[str, Any]:
        agents = self.store.list_agents()
        latest = {
            agent.agent_id: self.store.latest_agent_version(
                self.project.project_id, agent.agent_id)
            for agent in agents
        }
        items = []
        drifts = []
        for agent in agents:
            row = latest.get(agent.agent_id) or {}
            item = {
                "agent_id": agent.agent_id, "name": agent.name,
                "version": agent.version, "current_fingerprint":
                    self.store.agent_fingerprint(agent),
                "latest": row,
                "compat_status": row.get("compat_status", "unknown"),
                "compat_label": COMPAT_LABELS.get(row.get("compat_status", ""), "未记录"),
                "history_count": len(self.store.list_agent_versions(
                    self.project.project_id, agent_id=agent.agent_id, limit=1000)),
            }
            items.append(item)
            if item["compat_status"] == "drift":
                drifts.append({
                    "agent_id": agent.agent_id, "name": agent.name,
                    "version": agent.version,
                    "changes": (row.get("changes") or {}),
                    "recommendation": "复核适配器配置与能力清单，必要时回滚 Agent 版本或重新注册接入方式",
                })
        return {
            "agents": items, "drifts": drifts,
            "drift_count": len(drifts),
            "labels": COMPAT_LABELS,
            "total_versions": len(self.store.list_agent_versions(
                self.project.project_id, limit=10000)),
        }

    def history(self, agent_id: str = "", *, limit: int = 200) -> dict[str, Any]:
        rows = self.store.list_agent_versions(
            self.project.project_id, agent_id=agent_id, limit=limit)
        for row in rows:
            row["compat_label"] = COMPAT_LABELS.get(row.get("compat_status", ""), "")
        return {
            "agent_id": agent_id, "versions": rows, "count": len(rows),
            "labels": COMPAT_LABELS,
        }

    def drift(self) -> dict[str, Any]:
        status = self.status()
        return {"drifts": status["drifts"], "count": status["drift_count"],
                "checked_at": status}


__all__ = ["AgentVersionService", "COMPAT_LABELS"]
