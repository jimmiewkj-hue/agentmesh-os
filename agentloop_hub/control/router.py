"""Capability Router: route each task to the best available local Agent.

需求整理-v1 模块 5 要求系统实时评估 ``能力 / 成本 / 速度 / 成功率``，自动
选择执行节点，而不是让用户手工挑人。评分是确定性的、可解释的：

* 能力（硬门槛）：任务类型映射到能力集合，不满足直接淘汰；
* 成功率：历史完成/失败 + 拉普拉斯平滑；
* 验证通过率：独立验证结论；
* 速度：平均执行时长与参考值比较；
* 成本：平均每任务费用与参考值比较；
* 访问等级作为极小的稳定加成，只在同分时起作用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..config import HubConfig
from ..connectors import (
    capability_evidence, derive_capability_list, is_auto_executable,
    missing_capabilities, required_capabilities_for,
)
from ..models import AgentRecord, Project, Task, TaskSpec
from ..storage.repo import Store
from ..util import new_id, now_iso


PRIOR_SUCCESS = 0.5


@dataclass
class RoutingDecision:
    agent: AgentRecord
    score: float
    breakdown: dict[str, Any]
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    required: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent.agent_id,
            "agent_name": self.agent.name,
            "score": round(self.score, 4),
            "breakdown": self.breakdown,
            "candidates": self.candidates,
            "reason": self.reason,
            "required_capabilities": self.required,
        }


class CapabilityRouter:
    def __init__(self, store: Store, project: Project, config: HubConfig):
        self.store = store
        self.project = project
        self.config = config
        self.last_rejected: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def select(self, task: Task, *, preferred_agent: str = "",
               record: bool = True) -> RoutingDecision | None:
        decision = self._choose(task.spec, preferred_agent=preferred_agent or task.assigned_agent)
        if decision and record and task.uid:
            self.store.record_routing(
                project_id=self.project.project_id, decision_id=new_id("route"),
                agent_id=decision.agent.agent_id, score=decision.score,
                breakdown={**decision.breakdown, "required": decision.required},
                candidates=decision.candidates, reason=decision.reason,
                run_id=task.run_id, task_uid=task.uid, task_id=task.task_id,
            )
        return decision

    def rank_task(self, task: Task, *, limit: int = 50) -> dict[str, Any]:
        decision = self._choose(task.spec, preferred_agent="")
        return {
            "task_id": task.task_id,
            "task_type": task.spec.type,
            "required_capabilities": required_capabilities_for(task.spec),
            "selected": decision.to_dict() if decision else None,
            "candidates": decision.candidates if decision else self.last_rejected,
            "rejected": [] if decision else self.last_rejected,
            "generated_at": now_iso(),
        }

    def rank_spec(self, spec: TaskSpec) -> dict[str, Any]:
        decision = self._choose(spec, preferred_agent="")
        return {
            "task_id": spec.task_id,
            "task_type": spec.type,
            "required_capabilities": required_capabilities_for(spec),
            "selected": decision.to_dict() if decision else None,
            "candidates": decision.candidates if decision else self.last_rejected,
            "rejected": [] if decision else self.last_rejected,
            "generated_at": now_iso(),
        }

    def scores(self) -> dict[str, Any]:
        stats = self._agent_stats()
        agents = self.store.list_agents()
        return {
            "agents": [
                {
                    "agent_id": a.agent_id, "name": a.name,
                    "capabilities": derive_capability_list(a),
                    "capability_evidence": capability_evidence(a),
                    "stats": stats.get(a.agent_id, {}),
                }
                for a in agents
            ],
            "weights": dict(self.config.router_weights),
        }

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------
    def _choose(self, spec: TaskSpec, *, preferred_agent: str = "") -> RoutingDecision | None:
        required = required_capabilities_for(spec)
        agents = self.store.list_agents()
        stats = self._agent_stats()
        main = self.store.get_main_node(self.project.project_id)
        main_id = (main or {}).get("agent_id", "")
        eligible: list[tuple[AgentRecord, dict[str, Any]]] = []
        rejected: list[dict[str, Any]] = []
        for agent in agents:
            reasons: list[str] = []
            if agent.adapter_type == "session_only":
                reasons.append("仅会话读取，不能执行任务")
            if agent.status in ("offline", "error"):
                reasons.append(f"状态 {agent.status}")
            if self.config.router_require_auto_executable \
                    and not is_auto_executable(agent) \
                    and not self.config.router_allow_manual_relay:
                reasons.append("不可自动执行（需人工在 GUI/MCP 客户端接力）")
            missing = missing_capabilities(agent, required)
            if missing:
                reasons.append(f"缺少能力 {missing}")
            if reasons:
                rejected.append({"agent_id": agent.agent_id, "score": 0.0, "rejected": reasons})
                continue
            if self.config.router_exclude_main and agent.agent_id == main_id \
                    and agent.agent_id != preferred_agent:
                rejected.append({"agent_id": agent.agent_id, "score": 0.0,
                                 "rejected": ["主节点保留用于决策与汇总"]})
                continue
            score, breakdown = self._score(agent, spec, stats.get(agent.agent_id, {}))
            if agent.agent_id == spec.suggested_agent:
                score += 0.03
                breakdown["suggested_bonus"] = 0.03
            eligible.append((agent, {"agent_id": agent.agent_id, "name": agent.name,
                                     "score": round(score, 4), "breakdown": breakdown}))
        eligible.sort(key=lambda item: (-item[1]["score"], len(item[1]["agent_id"]),
                                        item[1]["agent_id"]))
        if preferred_agent:
            preferred = next((a for a in agents if a.agent_id == preferred_agent), None)
            matched = next((entry for entry in eligible if entry[0].agent_id == preferred_agent), None)
            if preferred and matched:
                return RoutingDecision(
                    agent=preferred, score=matched[1]["score"],
                    breakdown={**matched[1]["breakdown"], "preferred": True},
                    candidates=[e[1] for e in eligible] + rejected,
                    reason="沿用任务已分配的 Agent（重试稳定性）",
                    required=required,
                )
        self.last_rejected = rejected
        if not eligible:
            return None
        agent, best = eligible[0]
        reason = "按能力/成功率/验证通过率/速度/成本综合评分最高"
        if len(eligible) == 1:
            reason = "唯一满足能力门槛的可用 Agent"
        return RoutingDecision(
            agent=agent, score=best["score"],
            breakdown={**best["breakdown"], "required": required},
            candidates=[e[1] for e in eligible] + rejected,
            reason=reason, required=required,
        )

    def _score(self, agent: AgentRecord, spec: TaskSpec,
               stats: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        weights = self.config.router_weights or {}
        success = self._success_score(stats)
        verification = self._verification_score(stats)
        speed = self._speed_score(stats)
        cost = self._cost_score(stats)
        capability = 1.0  # eligibility already enforced all required capabilities
        access = {"L0": 0.0, "L1": 0.25, "L2": 0.5, "L3": 0.85, "L4": 1.0}.get(agent.access_level, 0.0)
        score = (
            float(weights.get("capability", 0.40)) * capability
            + float(weights.get("success", 0.25)) * success
            + float(weights.get("verification", 0.15)) * verification
            + float(weights.get("speed", 0.10)) * speed
            + float(weights.get("cost", 0.10)) * cost
        )
        # Access level distinguishes otherwise identical providers; keep it small.
        score += 0.02 * access
        breakdown = {
            "capability": round(capability, 4),
            "success": round(success, 4),
            "verification": round(verification, 4),
            "speed": round(speed, 4),
            "cost": round(cost, 4),
            "access": round(access, 4),
            "tasks_total": stats.get("tasks_total", 0),
            "success_rate": round(success, 4),
            "avg_duration_ms": round(float(stats.get("avg_duration_ms", 0) or 0), 1),
            "avg_cost": round(float(stats.get("avg_cost", 0) or 0), 6),
        }
        return score, breakdown

    def _agent_stats(self) -> dict[str, dict[str, Any]]:
        tasks = self.store.list_tasks_by_project(self.project.project_id, limit=2000)
        usage = self.store.agent_usage_stats(self.project.project_id)
        out: dict[str, dict[str, Any]] = {}
        for task in tasks:
            agent_id = task.assigned_agent
            if not agent_id:
                continue
            item = out.setdefault(agent_id, {
                "tasks_total": 0, "completed": 0, "failed": 0,
                "verified": 0, "verification_passed": 0,
            })
            item["tasks_total"] += 1
            if task.status == "completed":
                item["completed"] += 1
            elif task.status in ("failed", "blocked", "rejected"):
                item["failed"] += 1
            if task.verification_status in ("passed", "warning", "failed"):
                item["verified"] += 1
                if task.verification_status in ("passed", "warning"):
                    item["verification_passed"] += 1
        for agent_id, u in usage.items():
            item = out.setdefault(agent_id, {
                "tasks_total": 0, "completed": 0, "failed": 0,
                "verified": 0, "verification_passed": 0,
            })
            records = int(u.get("records", 0) or 0)
            total_cost = float(u.get("total_cost", 0) or 0)
            total_tokens = int(u.get("total_tokens", 0) or 0)
            item["avg_duration_ms"] = float(u.get("avg_duration_ms", 0) or 0)
            item["total_tokens"] = total_tokens
            item["total_cost"] = total_cost
            denominator = max(1, item["tasks_total"] or records)
            item["avg_cost"] = total_cost / denominator
            item["avg_tokens"] = total_tokens / denominator
        return out

    def _success_score(self, stats: dict[str, Any]) -> float:
        total = int(stats.get("tasks_total", 0) or 0)
        completed = int(stats.get("completed", 0) or 0)
        if total <= 0:
            # Never let an unverified Agent outrank an Agent with observed
            # evidence merely because its history is empty.
            return float(self.config.router_untested_prior)
        observed = (completed + PRIOR_SUCCESS) / (total + 1.0)
        confidence = total / (total + max(1, int(self.config.router_sample_strength)))
        return 0.5 + confidence * (observed - 0.5)

    def _verification_score(self, stats: dict[str, Any]) -> float:
        verified = int(stats.get("verified", 0) or 0)
        passed = int(stats.get("verification_passed", 0) or 0)
        if verified <= 0:
            return float(self.config.router_untested_prior)
        observed = (passed + PRIOR_SUCCESS) / (verified + 1.0)
        confidence = verified / (verified + max(1, int(self.config.router_sample_strength)))
        return 0.5 + confidence * (observed - 0.5)

    def _speed_score(self, stats: dict[str, Any]) -> float:
        avg = float(stats.get("avg_duration_ms", 0) or 0)
        if avg <= 0:
            return PRIOR_SUCCESS
        return max(0.0, 1.0 - min(1.0, avg / max(1, self.config.router_slow_reference_ms)))

    def _cost_score(self, stats: dict[str, Any]) -> float:
        avg = float(stats.get("avg_cost", 0) or 0)
        if avg <= 0:
            return PRIOR_SUCCESS
        return max(0.0, 1.0 - min(1.0, avg / max(1e-9, self.config.router_cost_reference_usd)))


__all__ = ["RoutingDecision", "CapabilityRouter"]
