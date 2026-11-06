"""Context Bus: the single information path between Controller and Agents.

需求整理-v1 模块 3 的核心创新。所有 Prompt、Response、文件变更、任务状态、
知识与工作流事件都写入一条只追加、可按订阅游标消费的事件流；主节点只读取
结果摘要（result sync），而不是复制子节点的完整上下文。

设计要点：

* 【单写路径】Store 的状态写入自动 emit task_state/workflow/knowledge/
  file_change/governance/response，服务层只显式发布 prompt/response 摘要。
* 【订阅游标】每个 Agent/外部程序可以有自己的持久订阅，不会互相消费。
* 【成本纪律】单个事件超过 Token 上限时保留结构化摘要字段并标记截断。
* 【可审计】每个项目独立哈希链，UPDATE/DELETE 被数据库触发器拒绝。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from ..config import HubConfig
from ..errors import NotFoundError, ValidationError
from ..models import Project
from ..storage.repo import Store
from ..util import estimate_tokens, now_iso

BUS_KINDS: dict[str, str] = {
    "prompt": "Agent 收到的任务/规划提示",
    "response": "Agent 返回的结构化成果摘要",
    "file_change": "文件变更与 Patch",
    "task_state": "任务状态变化",
    "workflow": "运行/工作流状态",
    "knowledge": "记忆与知识变更",
    "decision": "决策记录",
    "governance": "审批与人类治理",
    "routing": "能力路由决策",
    "verification": "独立验证结论",
    "cost": "成本节省记录",
    "message": "历史会话摘要",
    "agent_status": "Agent 在线/健康状态",
    "project_index": "项目索引",
    "manual_action": "节点需要人工提交/审批（链路阻塞）",
}

DEFAULT_TOPIC: dict[str, str] = {
    "prompt": "agent.prompt",
    "response": "agent.response",
    "file_change": "project.files",
    "task_state": "task.state",
    "workflow": "workflow.state",
    "knowledge": "memory.knowledge",
    "decision": "memory.decision",
    "governance": "governance.approval",
    "routing": "router.decision",
    "verification": "verification.report",
    "cost": "cost.saving",
    "message": "sync.message",
    "agent_status": "agent.status",
    "project_index": "project.index",
    "manual_action": "workflow.manual_action",
}

SYNC_LEVEL_BY_KIND: dict[str, int] = {
    "message": 1,
    "task_state": 2,
    "project_index": 3,
    "knowledge": 4,
    "decision": 4,
    "workflow": 5,
    "manual_action": 5,
}


class ContextBus:
    def __init__(self, store: Store, project: Project, config: HubConfig):
        self.store = store
        self.project = project
        self.config = config

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------
    def publish(self, kind: str, payload: dict[str, Any], *, source: str = "control-plane",
                correlation_id: str = "", topic: str = "",
                sync_level: int | None = None) -> dict[str, Any]:
        if kind not in BUS_KINDS:
            raise ValidationError(f"未知总线事件类型: {kind}", details={"allowed": sorted(BUS_KINDS)})
        if not isinstance(payload, dict):
            raise ValidationError("总线事件 payload 必须是对象")
        compact = self._compact_payload(payload)
        tokens = estimate_tokens(json.dumps(compact, ensure_ascii=False, default=str))
        level = SYNC_LEVEL_BY_KIND.get(kind, 0) if sync_level is None else int(sync_level)
        return self.store.append_bus_event(
            self.project.project_id,
            topic=topic or DEFAULT_TOPIC[kind],
            kind=kind,
            source=source or "control-plane",
            payload=compact,
            correlation_id=correlation_id,
            sync_level=level,
            tokens=tokens,
        )

    def emit(self, kind: str, payload: dict[str, Any], *, source: str = "control-plane",
             correlation_id: str = "", topic: str = "",
             sync_level: int | None = None) -> dict[str, Any]:
        """Alias used by Store's event sink."""
        return self.publish(kind, payload, source=source, correlation_id=correlation_id,
                            topic=topic, sync_level=sync_level)

    def _compact_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        limit = max(256, int(self.config.bus_payload_max_tokens))
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if estimate_tokens(text) <= limit:
            return payload
        compact: dict[str, Any] = {"_truncated": True, "original_tokens": estimate_tokens(text)}
        for key, value in payload.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                if len(str(value)) <= 400:
                    compact[key] = value
                else:
                    compact[key] = str(value)[:400]
            elif isinstance(value, list):
                items = value[:20]
                compact[key] = [
                    item if isinstance(item, (str, int, float, bool)) else
                    str(item)[:300] for item in items
                ]
            elif isinstance(value, dict):
                compact[key] = {k: v for k, v in list(value.items())[:10]
                                if isinstance(v, (str, int, float, bool))}
        if estimate_tokens(json.dumps(compact, ensure_ascii=False, default=str)) > limit:
            compact = {
                "_truncated": True,
                "original_tokens": estimate_tokens(text),
                "digest": text[: limit * 3],
            }
        return compact

    # ------------------------------------------------------------------
    # Read / replay
    # ------------------------------------------------------------------
    def events(self, *, after_seq: int = 0, limit: int = 100, topic: str = "",
               kind: str = "", correlation_id: str = "") -> list[dict[str, Any]]:
        return self.store.list_bus_events(
            self.project.project_id, after_seq=after_seq, limit=limit,
            topic=topic, kind=kind, correlation_id=correlation_id,
        )

    def tail(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.store.list_bus_events(self.project.project_id, after_seq=0,
                                           limit=5000, topic="")
        return rows[-max(1, int(limit)):]

    def replay(self, *, kind: str = "", topic: str = "", after_seq: int = 0,
               limit: int = 500) -> dict[str, Any]:
        events = self.events(after_seq=after_seq, limit=limit, kind=kind, topic=topic)
        return {
            "events": events,
            "count": len(events),
            "last_seq": events[-1]["seq"] if events else after_seq,
            "append_only": True,
        }

    def stats(self) -> dict[str, Any]:
        stats = self.store.bus_stats(self.project.project_id)
        totals = stats.get("totals") or {}
        totals["retention_hint"] = int(self.config.bus_retention_events)
        totals["retention_exceeded"] = int(totals.get("events", 0)) > int(self.config.bus_retention_events)
        return stats

    def verify(self) -> dict[str, Any]:
        ok, errors = self.store.verify_bus_chain(self.project.project_id)
        return {"ok": ok, "errors": errors,
                "count": int((self.store.bus_stats(self.project.project_id).get("totals") or {}).get("events", 0))}

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------
    def subscribe(self, subscriber: str, *, topics: Iterable[str] | None = None,
                  kinds: Iterable[str] | None = None) -> dict[str, Any]:
        if not str(subscriber or "").strip():
            raise ValidationError("订阅者不能为空")
        topic_list = [str(t) for t in (topics or []) if str(t).strip()]
        kind_list = [str(k) for k in (kinds or []) if str(k).strip()]
        unknown = sorted(set(kind_list) - set(BUS_KINDS))
        if unknown:
            raise ValidationError(f"未知总线事件类型: {unknown}")
        return self.store.create_bus_subscription(
            self.project.project_id, subscriber, topics=topic_list, kinds=kind_list,
        )

    def poll(self, subscription_id: str, *, limit: int = 100) -> dict[str, Any]:
        subscription = self.store.get_bus_subscription(subscription_id)
        if not subscription or subscription["project_id"] != self.project.project_id:
            raise NotFoundError(f"总线订阅不存在: {subscription_id}")
        if not subscription.get("active"):
            raise ValidationError(f"总线订阅已停用: {subscription_id}")
        topic_filter = set(subscription.get("topics") or [])
        kind_filter = set(subscription.get("kinds") or [])
        scanned = self.store.list_bus_events(
            self.project.project_id, after_seq=int(subscription["cursor_seq"]),
            limit=5000,
        )
        matches = [
            event for event in scanned
            if (not topic_filter or event["topic"] in topic_filter)
            and (not kind_filter or event["kind"] in kind_filter)
        ]
        selected = matches[: max(1, int(limit))]
        # Advance only over events actually delivered.  A page must never move
        # the cursor past matching events it did not return.
        if selected:
            new_cursor = int(selected[-1]["seq"])
        elif scanned and not matches:
            # Non-matching events are intentionally skipped by this filter.
            new_cursor = int(scanned[-1]["seq"])
        else:
            new_cursor = int(subscription["cursor_seq"])
        if new_cursor > int(subscription["cursor_seq"]):
            self.store.update_bus_cursor(subscription_id, new_cursor)
        return {
            "subscription_id": subscription_id,
            "events": selected,
            "count": len(selected),
            "cursor": new_cursor,
            "acked_seq": int(subscription.get("acked_seq") or 0),
            "has_more": len(matches) > len(selected) or len(scanned) >= 5000,
            "polled_at": now_iso(),
        }

    def ack(self, subscription_id: str, seq: int) -> dict[str, Any]:
        subscription = self.store.get_bus_subscription(subscription_id)
        if not subscription or subscription["project_id"] != self.project.project_id:
            raise NotFoundError(f"总线订阅不存在: {subscription_id}")
        self.store.ack_bus_cursor(subscription_id, int(seq))
        updated = self.store.get_bus_subscription(subscription_id) or {}
        return {"subscription_id": subscription_id,
                "acked_seq": int(updated.get("acked_seq") or 0),
                "cursor": int(updated.get("cursor_seq") or 0)}

    def poll_subscriber(self, subscriber: str, *, limit: int = 100) -> dict[str, Any]:
        subscriptions = self.store.list_bus_subscriptions(self.project.project_id)
        match = next((s for s in subscriptions if s["subscriber"] == subscriber), None)
        if not match:
            raise NotFoundError(f"订阅者不存在: {subscriber}")
        return self.poll(match["subscription_id"], limit=limit)

    def unsubscribe(self, subscription_id: str) -> dict[str, Any]:
        if not self.store.get_bus_subscription(subscription_id):
            raise NotFoundError(f"总线订阅不存在: {subscription_id}")
        self.store.deactivate_bus_subscription(subscription_id)
        return {"status": "unsubscribed", "subscription_id": subscription_id}

    def subscriptions(self) -> list[dict[str, Any]]:
        return self.store.list_bus_subscriptions(self.project.project_id)

    # ------------------------------------------------------------------
    # Digest used by memory bootstrap / main-node summary
    # ------------------------------------------------------------------
    def digest(self, *, correlation_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
        events = self.store.list_bus_events(
            self.project.project_id, after_seq=0, limit=max(5000, int(limit) * 10),
            correlation_id=correlation_id,
        )
        events = events[-max(1, int(limit)):]
        return [
            {
                "seq": e["seq"], "kind": e["kind"], "topic": e["topic"],
                "source": e["source"], "correlation_id": e["correlation_id"],
                "created_at": e["created_at"], "payload": e["payload"],
            }
            for e in events
        ]

    def recent_digest(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.store.recent_bus_events(self.project.project_id, limit=max(1, limit))
        return [
            {
                "seq": e["seq"], "kind": e["kind"], "topic": e["topic"],
                "source": e["source"], "created_at": e["created_at"],
                "payload": e["payload"],
            }
            for e in rows
        ]


__all__ = ["BUS_KINDS", "DEFAULT_TOPIC", "SYNC_LEVEL_BY_KIND", "ContextBus"]
