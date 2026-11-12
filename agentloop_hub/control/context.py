"""Progressive context disclosure: summary -> findings -> evidence -> raw."""

from __future__ import annotations

import re
from typing import Any

from ..config import HubConfig
from ..models import Project, Task
from ..storage.repo import Store
from ..util import estimate_tokens, read_text, truncate


LEVELS = ("summary", "findings", "evidence", "raw")


def task_round_id(task_id: str) -> int:
    match = re.match(r"^R(\d+)_", str(task_id or ""))
    return int(match.group(1)) if match else 1


class ContextBuilder:
    def __init__(self, store: Store, project: Project, config: HubConfig):
        self.store = store
        self.project = project
        self.config = config

    def limit_for(self, level: str) -> int:
        return {
            "summary": self.config.context_summary_tokens,
            "findings": self.config.context_findings_tokens,
            "evidence": self.config.context_evidence_tokens,
            "raw": self.config.context_raw_tokens,
        }.get(level, self.config.context_summary_tokens)

    def task_detail(self, task: Task, *, level: str = "summary") -> dict[str, Any]:
        if level not in LEVELS:
            raise ValueError(f"未知上下文级别: {level}")
        result = self.store.get_result(task.uid)
        verification = self.store.latest_verification(task.uid)
        changes = self.store.list_changes(task_uid=task.uid)
        routing = self.store.latest_routing(task.uid)
        base: dict[str, Any] = {
            "task_uid": task.uid,
            "task_id": task.task_id,
            "title": task.spec.title,
            "type": task.spec.type,
            "status": task.status,
            "assigned_agent": task.assigned_agent,
            "attempts": task.attempts,
            "verification_status": task.verification_status,
            "objective": task.spec.objective,
            "scope": task.spec.scope,
            "out_of_scope": task.spec.out_of_scope,
            "acceptance_criteria": task.spec.acceptance_criteria,
            "token_budget": task.spec.token_budget,
            "priority": task.spec.priority,
            "patch_path": task.patch_path,
            "error": task.error,
        }
        if routing:
            base["routing"] = {
                "agent_id": routing["agent_id"], "score": routing["score"],
                "reason": routing["reason"], "breakdown": routing["breakdown"],
            }
        if result:
            payload = result["payload"]
            base["result"] = {
                "status": payload.get("status"),
                "summary": payload.get("summary"),
                "valid": result["valid"],
                "validation_error": result.get("validation_error", ""),
            }
        if verification:
            base["verification"] = {
                "verifier": verification["verifier_agent"],
                "status": verification["status"],
                "checks": verification["checks"],
                "warnings": verification["warnings"],
            }
        if level in ("findings", "evidence", "raw") and result:
            payload = result["payload"]
            base["findings"] = payload.get("findings", [])
            base["risks"] = payload.get("risks", [])
            base["unresolved_items"] = payload.get("unresolved_items", [])
            base["tests"] = payload.get("tests", [])
            base["files_changed"] = payload.get("files_changed", [])
        if level in ("evidence", "raw") and result:
            payload = result["payload"]
            base["evidence"] = payload.get("evidence", [])
            base["changes"] = [c.to_dict() for c in changes]
            if verification:
                base["verification"]["checks"] = verification["checks"]
                base["verification"]["warnings"] = verification["warnings"]
        if level == "raw":
            if result:
                base["raw_result"] = result["payload"]
            base["logs"] = read_text(task.task_dir + "/logs/agent.stdout.log")[-10000:] if task.task_dir else ""
            base["stderr"] = read_text(task.task_dir + "/logs/agent.stderr.log")[-10000:] if task.task_dir else ""
        return self._cap(base, self.limit_for(level))

    def upstream_results(self, task: Task) -> list[dict[str, Any]]:
        """Result sync: only compact digests from direct dependencies."""
        if not task.spec.dependencies:
            return []
        tasks = {t.task_id: t for t in self.store.list_tasks(task.run_id)}
        out: list[dict[str, Any]] = []
        for dep in task.spec.dependencies:
            upstream = tasks.get(dep)
            if not upstream or upstream.status != "completed":
                continue
            result = self.store.get_result(upstream.uid)
            if not result or not result.get("valid"):
                continue
            payload = result["payload"]
            out.append({
                "task_id": upstream.task_id,
                "agent_id": upstream.assigned_agent,
                "status": payload.get("status"),
                "summary": str(payload.get("summary", ""))[:500],
                "findings": list(payload.get("findings", []))[:20],
                "files_changed": list(payload.get("files_changed", []))[:50],
                "tests": list(payload.get("tests", []))[:20],
                "risks": list(payload.get("risks", []))[:10],
                "unresolved_items": list(payload.get("unresolved_items", []))[:10],
                "recommended_next_action": list(payload.get("recommended_next_action", []))[:10],
            })
        return out

    def planning_context(self, *, goal: str, memories: list[dict[str, Any]] | None = None,
                         recent_conversations: list[dict[str, Any]] | None = None,
                         agents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        decisions = self.store.list_decisions(self.project.project_id, limit=10)
        ctx = {
            "project": {
                "project_id": self.project.project_id,
                "name": self.project.name,
                "root": self.project.root,
                "branch": self.project.branch,
                "commit": self.project.commit,
                "tech_stack": self.project.tech_stack,
                "build_command": self.project.build_command,
                "test_command": self.project.test_command,
            },
            "user_goal": goal,
            "confirmed_constraints": [],
            "memories": memories or [],
            "recent_conversations": recent_conversations or [],
            "decisions": [
                {"content": d.content, "agent": d.agent_id, "model": d.model, "approved": d.approved}
                for d in decisions
            ],
            "agents": agents or [],
            "rules": {
                "implementation_scope_required": True,
                "result_is_structured": True,
                "executor_cannot_self_verify": True,
                "max_parallel": self.config.max_parallel,
            },
        }
        return self._cap(ctx, self.config.context_summary_tokens)

    def summary_context(self, *, run: dict[str, Any], plan: dict[str, Any],
                        tasks: list[Task], verification_by_task: dict[str, dict[str, Any]] | None = None,
                        changes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        # 2026-11-07 defect #8: the summary prompt was capped by dropping list
        # tails, so the newest round's tasks/changes (R2_T001/R2_REVIEW) were
        # removed first and the main node judged on round-1 evidence.  Latest
        # round now comes first, per-entry fields are bounded, and _cap drops
        # the oldest entries first.
        ordered = sorted(tasks, key=lambda t: (-task_round_id(t.task_id),
                                               str(t.task_id)))
        rounds_present = sorted({task_round_id(t.task_id) for t in ordered},
                                reverse=True)
        results = []
        for task in ordered:
            result = self.store.get_result(task.uid)
            payload = (result or {}).get("payload", {})
            verification = (verification_by_task or {}).get(task.uid) or self.store.latest_verification(task.uid)
            results.append({
                "task_id": task.task_id,
                "round": task_round_id(task.task_id),
                "title": task.spec.title,
                "status": task.status,
                "agent": task.assigned_agent,
                "attempts": task.attempts,
                "summary": str(payload.get("summary") or "")[:1500],
                "files_changed": [str(p) for p in (payload.get("files_changed") or [])][:30],
                "risks": [str(p) for p in (payload.get("risks") or [])][:10],
                "unresolved_items": [str(p) for p in
                                     (payload.get("unresolved_items") or [])][:12],
                "tests": (payload.get("tests") or [])[:10],
                "verification": {
                    "status": (verification or {}).get("status", "pending"),
                    "warnings": [str(w) for w in
                                 (verification or {}).get("warnings", [])][:10],
                },
            })

        def _change_round(row: dict[str, Any]) -> int:
            return task_round_id(str(row.get("task_uid") or "").rsplit(":", 1)[-1])

        change_rows = sorted(list(changes or []), key=_change_round, reverse=True)
        compact_changes = [
            {**row, "files": [str(p) for p in (row.get("files") or [])][:40]}
            for row in change_rows[:60]
        ]
        ctx = {
            "goal": plan.get("goal"),
            "constraints": plan.get("constraints", []),
            "run_id": run.get("run_id"),
            "latest_round": rounds_present[0] if rounds_present else 1,
            "rounds_present": rounds_present,
            "ordering": "tasks/changes 按最新轮次优先排列；预算不足时先丢弃最旧轮次",
            "tasks": results,
            "changes": compact_changes,
            "instructions": "只依据下列摘要、证据索引和验证结论给出最终判断；不得假设未提供的事实。",
        }
        return self._cap(ctx, self.config.context_summary_tokens)

    def handoff_context(self, *, build_handoff_fn) -> dict[str, Any]:
        # Kept separate in handoff.py; this hook avoids duplicating DB queries here.
        return build_handoff_fn()

    @staticmethod
    def _shrink_list_entry(item: list) -> bool:
        """Shrink nested fields of the oldest dict entry in place.

        Returns True when something was reduced.  Summary contexts order
        newest rounds first; shrinking the tail entry keeps the newest round
        intact while the budget shrinks.
        """
        for entry in reversed(item):
            if not isinstance(entry, dict):
                continue
            for key, value in entry.items():
                if isinstance(value, list) and len(value) > 1:
                    value.pop()
                    return True
                if isinstance(value, str) and len(value) > 200:
                    entry[key] = truncate(value, max(200, len(value) // 2))
                    return True
                if isinstance(value, dict):
                    for inner_key, inner in value.items():
                        if isinstance(inner, list) and len(inner) > 1:
                            inner.pop()
                            return True
        return False

    @staticmethod
    def _cap(value: Any, token_limit: int) -> Any:
        import copy
        import json
        text = json.dumps(value, ensure_ascii=False, default=str)
        if estimate_tokens(text) <= token_limit:
            return value
        if not isinstance(value, dict):
            return {
                "_truncated": True,
                "token_limit": token_limit,
                "content": truncate(text, token_limit * 3),
            }
        out = copy.deepcopy(value)
        out["_truncated"] = True
        # Shrink the largest fields first, preserving a valid JSON object.
        keys = sorted(out.keys(), key=lambda k: len(json.dumps(out[k], ensure_ascii=False, default=str)), reverse=True)
        for key in keys:
            if estimate_tokens(json.dumps(out, ensure_ascii=False, default=str)) <= token_limit:
                break
            item = out[key]
            if isinstance(item, list):
                # Never drop the first entry (newest round / primary subject);
                # shrink nested fields of the tail first, then drop tail items.
                while item and estimate_tokens(
                        json.dumps(out, ensure_ascii=False, default=str)) > token_limit:
                    if ContextBuilder._shrink_list_entry(item):
                        continue
                    if len(item) > 1:
                        item.pop()
                    else:
                        break
            elif isinstance(item, str) and len(item) > 200:
                out[key] = truncate(item, max(200, len(item) // 2))
            elif isinstance(item, dict):
                out[key] = {"_truncated": True,
                            "content": truncate(json.dumps(item, ensure_ascii=False, default=str), token_limit * 2)}
            else:
                out[key] = ""
        if estimate_tokens(json.dumps(out, ensure_ascii=False, default=str)) > token_limit:
            fallback = {"_truncated": True, "token_limit": token_limit,
                        "content": truncate(json.dumps(value, ensure_ascii=False, default=str),
                                            token_limit * 3)}
            for key, item in value.items():
                if isinstance(item, (str, int, float, bool)) and len(str(item)) <= 80:
                    fallback[key] = item
            return fallback
        return out
