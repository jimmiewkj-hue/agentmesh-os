"""Unified sync across five levels required by 需求整理-v1.

Level 1 消息：历史会话导入 + message 记忆；
Level 2 任务：task_state 事件与记忆；
Level 3 上下文：项目索引与代码结构；
Level 4 知识：knowledge/decision 记忆；
Level 5 工作流：运行阶段、进度与待审批。

同步是确定性的：不推断事实、不复制完整聊天、不上传数据。
"""

from __future__ import annotations

from typing import Any

from ..models import MEMORY_SYNC_LEVEL, AgentRecord, Project
from ..storage.repo import Store
from ..util import now_iso
from .bus import ContextBus
from .memory import MemoryService
from ..sessions.service import SessionImportService


SYNC_LEVEL_LABELS = {
    1: "消息（历史会话）",
    2: "任务状态",
    3: "上下文（项目索引/代码结构）",
    4: "知识（问题解决/决策）",
    5: "工作流状态",
}


class SyncService:
    def __init__(self, store: Store, project: Project, memory: MemoryService,
                 sessions: SessionImportService, bus: ContextBus | None = None):
        self.store = store
        self.project = project
        self.memory = memory
        self.sessions = sessions
        self.bus = bus

    def status(self) -> dict[str, Any]:
        memories = self.store.list_memories(self.project.project_id, limit=10000)
        level_counts: dict[int, int] = {level: 0 for level in SYNC_LEVEL_LABELS}
        for memory in memories:
            level = MEMORY_SYNC_LEVEL.get(memory.type, 0)
            if level:
                level_counts[level] = level_counts.get(level, 0) + 1
        conversations = self.store.list_conversations(self.project.project_id, limit=1000)
        tasks = self.store.list_tasks_by_project(self.project.project_id, limit=1000)
        latest = self.store.latest_run(self.project.project_id)
        return {
            "levels": {
                level: {
                    "label": label,
                    "memory_records": level_counts.get(level, 0),
                }
                for level, label in SYNC_LEVEL_LABELS.items()
            },
            "conversations": len(conversations),
            "tasks_tracked": len(tasks),
            "workflow": {
                "run_id": latest["run_id"] if latest else "",
                "status": latest["status"] if latest else "no_run",
                "acceptance_status": latest.get("acceptance_status", "") if latest else "",
                "release_status": latest.get("release_status", "") if latest else "",
            },
            "bus": self.bus.stats() if self.bus else {},
            "generated_at": now_iso(),
        }

    def run(self, *, agent_id: str = "", limit: int = 20,
            include_unscoped: bool = False) -> dict[str, Any]:
        project_index = self.memory.seed_project_index()
        code_structure = self.memory.seed_code_structure()
        imported: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        agents = self.store.list_agents()
        if agent_id:
            agents = [a for a in agents if a.agent_id == agent_id]
        for agent in agents:
            if not agent.capabilities.get("session_read"):
                continue
            if not agent.adapter_config.get("session_reader"):
                continue
            try:
                result = self.sessions.import_for_agent(
                    agent, limit=limit, include_unscoped=include_unscoped,
                )
            except Exception as exc:
                skipped.append({"agent_id": agent.agent_id, "reason": str(exc)[:300]})
                continue
            for record in result.records:
                self.memory.sync_conversation(record)
            imported.append({
                "agent_id": agent.agent_id, "imported": result.imported,
                "skipped": result.skipped,
            })
        latest = self.store.latest_run(self.project.project_id)
        if latest:
            self.memory.sync_task_state(latest["run_id"])
            self.memory.sync_workflow(latest["run_id"])
        report = {
            "status": "synced",
            "generated_at": now_iso(),
            "project_index": project_index.memory_id,
            "code_structure": code_structure.memory_id,
            "sessions": imported,
            "skipped_agents": skipped,
            "levels": self.status()["levels"],
            "conversations": len(self.store.list_conversations(self.project.project_id, limit=1000)),
        }
        self.store.append_audit(self.project.project_id, "sync-service", "sync.completed",
                                payload={"sessions": imported, "skipped": skipped,
                                         "levels": list(report["levels"])})
        if self.bus:
            self.bus.publish("workflow", {
                "phase": "sync.completed", "sessions": imported,
                "project_index": project_index.memory_id,
                "code_structure": code_structure.memory_id,
            }, source="sync-service", sync_level=5)
        return report

    def bootstrap(self, *, agent_id: str = "", scope: str = "") -> dict[str, Any]:
        packet = self.memory.bootstrap(scope=scope, agent_id=agent_id)
        packet["sync_status"] = self.status()
        return packet

    def agent_ready(self, agent: AgentRecord) -> dict[str, Any]:
        """Packet an Agent should read before it starts working."""
        packet = self.bootstrap(agent_id=agent.agent_id)
        packet["connector"] = {
            "agent_id": agent.agent_id, "name": agent.name,
            "capabilities": list(agent.capabilities or {}),
        }
        return packet


__all__ = ["SYNC_LEVEL_LABELS", "SyncService"]
