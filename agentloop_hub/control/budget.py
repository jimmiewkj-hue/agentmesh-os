"""Token budget and cost accounting."""

from __future__ import annotations

from typing import Any

from ..adapters.base import ExecutionUsage
from ..config import HubConfig
from ..models import Project, Task, UsageRecord
from ..storage.repo import Store
from ..util import new_id, now_iso


class BudgetManager:
    def __init__(self, store: Store, project: Project, config: HubConfig):
        self.store = store
        self.project = project
        self.config = config

    @property
    def project_budget(self) -> int:
        extra = self.project.extra or {}
        try:
            value = int(extra.get("token_budget") or self.config.default_project_token_budget)
        except (TypeError, ValueError):
            value = self.config.default_project_token_budget
        return max(0, value)

    def _quota_tokens(self, totals: dict[str, Any]) -> int:
        if not self.config.budget_billable_only:
            return int(totals.get("total_tokens", 0) or 0)
        return int(totals.get("input_tokens", 0) or 0) + \
            int(totals.get("output_tokens", 0) or 0) + \
            int(totals.get("reasoning_tokens", 0) or 0)

    def used_project(self) -> int:
        return self._quota_tokens(self.store.usage_totals(self.project.project_id))

    def used_run(self, run_id: str) -> int:
        return self._quota_tokens(self.store.usage_totals(self.project.project_id, run_id=run_id))

    def used_task(self, task_uid: str) -> int:
        return self._quota_tokens(self.store.usage_totals(self.project.project_id, task_uid=task_uid))

    def remaining(self) -> int:
        if not self.project_budget:
            return -1
        return max(0, self.project_budget - self.used_project())

    def can_start(self, task: Task) -> tuple[bool, str]:
        # Budget is an advisory statistic by default.  Starting/stopping a task
        # is always a human decision; only an explicit budget_hard_stop=True
        # turns the counter into a gate.
        if not self.config.budget_hard_stop:
            return True, ""
        if not self.project_budget:
            return True, ""
        used = self.used_project()
        if used >= self.project_budget:
            return False, f"项目预算已耗尽（已用 {used} tokens / 预算 {self.project_budget}）"
        if task.spec.token_budget and used + task.spec.token_budget > self.project_budget:
            return False, (
                f"任务预算 {task.spec.token_budget} 超出项目剩余预算 "
                f"{max(0, self.project_budget - used)} tokens"
            )
        return True, ""

    def task_over_budget(self, task: Task) -> tuple[bool, int]:
        allowed = int(task.spec.token_budget or 0)
        used = self.used_task(task.uid)
        return (allowed > 0 and used > allowed), used

    def record(self, task: Task, agent_id: str, model: str, usage: ExecutionUsage, *,
               duration_ms: int, run_id: str = "", status: str = "ok") -> UsageRecord:
        usage.normalized()
        cost = float(usage.cost or 0.0)
        estimated = bool(usage.estimated)
        if cost <= 0 and (usage.input_tokens or usage.output_tokens):
            price = self.config.price_for(model)
            cost = (usage.input_tokens / 1_000_000) * price["input"] + \
                   (usage.output_tokens / 1_000_000) * price["output"]
            estimated = True
        record = UsageRecord(
            record_id=new_id("use"),
            project_id=self.project.project_id,
            agent_id=agent_id,
            model=model,
            task_uid=task.uid,
            run_id=run_id or task.run_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            total_tokens=usage.total_tokens,
            tool_calls=usage.tool_calls,
            retries=max(0, task.attempts - 1),
            duration_ms=duration_ms,
            cost=round(cost, 6),
            estimated=estimated,
            status=status,
            created_at=now_iso(),
        )
        self.store.add_usage(record)
        return record

    def snapshot(self, *, run_id: str = "") -> dict[str, Any]:
        totals = self.store.usage_totals(self.project.project_id, run_id=run_id)
        used = self.used_project()
        ratio = (used / self.project_budget) if self.project_budget else 0.0
        warn = bool(self.project_budget) and ratio >= float(self.config.budget_warn_ratio)
        return {
            "project_budget": self.project_budget,
            "used_project": used,
            "used_run": self.used_run(run_id) if run_id else 0,
            "remaining": self.remaining(),
            "billable_only": bool(self.config.budget_billable_only),
            "billable_tokens": used,
            "cached_tokens": int(totals.get("cached_tokens", 0) or 0),
            "used_ratio": round(ratio, 4),
            "warning": ("预算已使用 " + str(round(ratio * 100, 1)) + "%") if warn else "",
            "totals": totals,
            "by_agent": self.store.usage_by_agent(self.project.project_id, run_id=run_id),
            "by_task": self.store.usage_by_task(self.project.project_id, run_id=run_id),
        }
