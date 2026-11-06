"""Cost Optimizer: context compression, semantic cache, reuse and benchmark.

需求整理-v1 模块 6 要求把同样的任务从 100 美元压到 10 美元，手段是：

1. Context Compression —— 主节点/子节点只拿必要上下文，超出预算自动裁剪；
2. Semantic Cache —— 只读任务在同一文件基线下按意图相似度复用成果；
3. Intermediate Result Reuse —— 依赖任务只接收上游成果摘要，不复制全上下文；
4. Agent Benchmark —— 用真实任务数据评估成功率、验证通过率、速度与成本。

所有节省都写入 ``savings_ledger``，可在 CLI/Web 中审计，不是口头承诺。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from ..config import HubConfig
from ..connectors import TASK_CAPABILITY_REQUIREMENTS, derive_capability_list
from ..models import Project, ResultPackage, Task, pattern_overlap
from ..storage.repo import Store
from ..util import estimate_tokens, new_id, now_iso, sha1_hex, stable_json
from .context import ContextBuilder


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[a-z0-9_]{2,}")
_PUNCT_RE = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff]+")


def normalize_intent(text: str) -> str:
    return _PUNCT_RE.sub(" ", str(text or "").lower()).strip()


def intent_tokens(text: str) -> set[str]:
    normalized = normalize_intent(text)
    tokens = set(_WORD_RE.findall(normalized))
    cjk = "".join(_CJK_RE.findall(normalized))
    if len(cjk) >= 2:
        tokens.update(cjk[i:i + 2] for i in range(len(cjk) - 1))
    elif cjk:
        tokens.add(cjk)
    return {t for t in tokens if t}


def similarity(a: str, b: str) -> float:
    ta, tb = intent_tokens(a), intent_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class CostOptimizer:
    def __init__(self, store: Store, project: Project, config: HubConfig):
        self.store = store
        self.project = project
        self.config = config

    # ------------------------------------------------------------------
    # Signatures / semantic cache
    # ------------------------------------------------------------------
    def signature(self, task: Task) -> str:
        spec = task.spec
        return " | ".join([
            spec.type,
            normalize_intent(spec.objective),
            normalize_intent(" ".join(spec.acceptance_criteria)),
            normalize_intent(" ".join(spec.scope)),
            normalize_intent(" ".join(spec.out_of_scope)),
        ])

    def semantic_reference(self, task: Task, file_hash: str) -> dict[str, Any] | None:
        """Return a similar prior result as a *reference*, never as a result.

        Approximate similarity cannot prove the same intent (``>0`` and ``<0``
        normalize identically), so a match is not recorded as a hit or a
        saving and is never returned as a successful payload.
        """
        if task.spec.writable or not file_hash:
            return None
        entries = self.store.semantic_candidates(
            self.project.project_id, task.spec.type, file_hash,
            limit=int(self.config.semantic_cache_max_entries),
        )
        signature = self.signature(task)
        best: tuple[float, dict[str, Any]] | None = None
        for entry in entries:
            score = similarity(signature, entry.get("signature", ""))
            if best is None or score > best[0]:
                best = (score, entry)
        if not best or best[0] < float(self.config.semantic_cache_threshold):
            return None
        score, entry = best
        return {
            "cache_key": entry["cache_key"],
            "similarity": round(score, 4),
            "payload": entry.get("payload", {}),
            "source_task": entry.get("objective", ""),
            "reference_only": True,
        }

    def semantic_store(self, task: Task, file_hash: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if task.spec.writable or not file_hash or not isinstance(payload, dict):
            return None
        signature = self.signature(task)
        cache_key = sha1_hex(stable_json({
            "project_id": self.project.project_id,
            "task_type": task.spec.type,
            "signature": signature,
            "file_hash": file_hash,
        }))
        self.store.semantic_set(
            cache_key, self.project.project_id, task.spec.type, signature,
            task.spec.objective[:500], file_hash, payload,
        )
        return {"cache_key": cache_key, "similarity": 1.0}

    # ------------------------------------------------------------------
    # Intermediate result reuse
    # ------------------------------------------------------------------
    def intermediate_hints(self, task: Task, *, limit: int = 3) -> list[dict[str, Any]]:
        """Return compact digests of previous similar analysis results.

        Only the structured digest travels; the full result stays in the
        task ledger. This is the "result sync" the requirement asks for.
        """
        signature = self.signature(task)
        hints: list[tuple[float, dict[str, Any]]] = []
        tasks = self.store.list_tasks_by_project(self.project.project_id, limit=500)
        for other in tasks:
            if other.uid == task.uid or other.status != "completed":
                continue
            if other.spec.type not in ("analysis", "review", "architecture"):
                continue
            if task.spec.scope and other.spec.scope and not pattern_overlap(task.spec.scope, other.spec.scope):
                continue
            score = similarity(signature, self.signature(other))
            if score < float(self.config.semantic_cache_threshold):
                continue
            result = self.store.get_result(other.uid)
            if not result or not result.get("valid"):
                continue
            payload = result["payload"]
            digest = ResultPackage(payload).digest()
            hints.append((score, {
                "task_id": other.task_id, "uid": other.uid, "agent_id": other.assigned_agent,
                "similarity": round(score, 4), "digest": digest,
            }))
        hints.sort(key=lambda item: (-item[0], item[1]["task_id"], item[1]["uid"]))
        selected = [item[1] for item in hints[:limit]]
        if selected:
            saved = 0
            for item in hints[:limit]:
                full = self.store.get_result(item[1]["uid"])
                if full:
                    saved += max(0, estimate_tokens(json.dumps(full["payload"], ensure_ascii=False))
                                 - estimate_tokens(json.dumps(item[1]["digest"], ensure_ascii=False)))
            self.record_saving(
                "intermediate_reuse", saved, task_uid=task.uid, run_id=task.run_id,
                detail={"hints": [h["task_id"] for h in selected]},
            )
        return selected

    # ------------------------------------------------------------------
    # Context compression
    # ------------------------------------------------------------------
    def compress_context(self, context: dict[str, Any], token_limit: int, *,
                         kind: str = "context_compression", task_uid: str = "",
                         run_id: str = "") -> dict[str, Any]:
        before = estimate_tokens(json.dumps(context, ensure_ascii=False, default=str))
        compressed = ContextBuilder._cap(context, token_limit)
        after = estimate_tokens(json.dumps(compressed, ensure_ascii=False, default=str))
        if before > after:
            self.record_saving(kind, before - after, task_uid=task_uid, run_id=run_id,
                               detail={"before_tokens": before, "after_tokens": after,
                                       "limit": token_limit})
        return compressed

    # ------------------------------------------------------------------
    # Savings ledger
    # ------------------------------------------------------------------
    def record_saving(self, kind: str, tokens_saved: int, *, cost_saved: float = 0.0,
                      task_uid: str = "", run_id: str = "",
                      detail: dict[str, Any] | None = None) -> None:
        if int(tokens_saved) <= 0 and float(cost_saved) <= 0:
            return
        self.store.add_saving(
            saving_id=new_id("save"), project_id=self.project.project_id, kind=kind,
            tokens_saved=max(0, int(tokens_saved)), cost_saved=max(0.0, float(cost_saved)),
            task_uid=task_uid, run_id=run_id, detail=detail or {},
        )

    def savings(self) -> dict[str, Any]:
        return {
            "totals": self.store.savings_totals(self.project.project_id),
            "by_kind": self.store.savings_by_kind(self.project.project_id),
            "recent": self.store.list_savings(self.project.project_id, limit=50),
            "semantic_cache": self.store.semantic_stats(self.project.project_id),
        }

    # ------------------------------------------------------------------
    # Agent benchmark
    # ------------------------------------------------------------------
    def benchmark(self, *, persist: bool = True) -> dict[str, Any]:
        tasks = self.store.list_tasks_by_project(self.project.project_id, limit=2000)
        usage = self.store.agent_usage_stats(self.project.project_id)
        agents = self.store.list_agents()
        rows: list[dict[str, Any]] = []
        for agent in agents:
            assigned = [t for t in tasks if t.assigned_agent == agent.agent_id]
            completed = [t for t in assigned if t.status == "completed"]
            failed = [t for t in assigned if t.status in ("failed", "blocked", "rejected")]
            verified = [t for t in assigned if t.verification_status in ("passed", "warning", "failed")]
            verification_passed = [t for t in verified if t.verification_status in ("passed", "warning")]
            record = usage.get(agent.agent_id, {})
            records = int(record.get("records", 0) or 0)
            total_cost = float(record.get("total_cost", 0) or 0)
            total_tokens = int(record.get("total_tokens", 0) or 0)
            avg_duration = float(record.get("avg_duration_ms", 0) or 0)
            success_rate = (len(completed) + 0.5) / (len(assigned) + 1.0)
            verification_rate = (len(verification_passed) + 0.5) / (len(verified) + 1.0)
            denominator = max(1, len(assigned) or records)
            avg_cost = total_cost / denominator
            avg_tokens = total_tokens / denominator
            cost_score = 0.5 if not record else max(0.0, 1.0 - min(
                1.0, avg_cost / max(1e-9, self.config.router_cost_reference_usd)))
            speed_score = 0.5 if avg_duration <= 0 else max(
                0.0, 1.0 - min(1.0, avg_duration / max(1, self.config.router_slow_reference_ms)))
            score = (0.40 * success_rate + 0.20 * verification_rate
                     + 0.20 * cost_score + 0.20 * speed_score)
            snapshot = {
                "agent_id": agent.agent_id, "name": agent.name,
                "capabilities": derive_capability_list(agent),
                "tasks_total": len(assigned), "tasks_completed": len(completed),
                "tasks_failed": len(failed), "verification_total": len(verified),
                "verification_passed": len(verification_passed),
                "success_rate": round(success_rate, 4),
                "verification_pass_rate": round(verification_rate, 4),
                "avg_tokens": round(avg_tokens, 1), "avg_cost": round(avg_cost, 6),
                "avg_duration_ms": round(avg_duration, 1), "score": round(score, 4),
                "records": records, "total_tokens": total_tokens, "total_cost": round(total_cost, 6),
            }
            rows.append(snapshot)
            if persist:
                self.store.save_benchmark(
                    benchmark_id=new_id("bench"), project_id=self.project.project_id,
                    agent_id=agent.agent_id, snapshot=snapshot, score=score,
                )
        rows.sort(key=lambda r: (-float(r["score"]), r["agent_id"]))
        cheapest = self._cheapest_sufficient(rows)
        return {
            "generated_at": now_iso(),
            "agents": rows,
            "cheapest_sufficient": cheapest,
            "weights": {"success": 0.40, "verification": 0.20, "cost": 0.20, "speed": 0.20},
        }

    def _cheapest_sufficient(self, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for task_type, required in TASK_CAPABILITY_REQUIREMENTS.items():
            eligible = [
                row for row in rows
                if set(required).issubset(set(row.get("capabilities") or []))
                and (row.get("tasks_total", 0) == 0 or float(row.get("success_rate", 0)) >= 0.5)
            ]
            eligible.sort(key=lambda row: (
                row.get("avg_cost", 0) if row.get("records", 0) else float("inf"),
                row.get("avg_tokens", 0) if row.get("records", 0) else float("inf"),
                row["agent_id"],
            ))
            out[task_type] = {
                "required_capabilities": required,
                "recommended": [row["agent_id"] for row in eligible[:3]],
                "basis": "能力满足 -> 成功率>=0.5 -> 每任务平均成本/Token 最低",
            }
        return out

    def report(self, *, run_id: str = "") -> dict[str, Any]:
        savings = self.savings()
        savings["totals_run"] = self.store.savings_totals(self.project.project_id, run_id=run_id) if run_id else {}
        benchmarks = self.store.latest_benchmarks(self.project.project_id)
        return {
            "savings": savings,
            "benchmark": [
                {"agent_id": b["agent_id"], "score": b["score"],
                 "snapshot": b["snapshot"], "created_at": b["created_at"]}
                for b in benchmarks
            ],
            "semantic_cache": self.store.semantic_stats(self.project.project_id),
            "generated_at": now_iso(),
        }

    @staticmethod
    def _payload_tokens(payload: dict[str, Any]) -> int:
        usage = payload.get("token_usage") if isinstance(payload, dict) else None
        if isinstance(usage, dict):
            total = int(usage.get("total", 0) or 0)
            if not total:
                total = int(usage.get("input", 0) or 0) + int(usage.get("output", 0) or 0)
            if total:
                return total
        return estimate_tokens(json.dumps(payload, ensure_ascii=False, default=str))


__all__ = ["CostOptimizer", "intent_tokens", "normalize_intent", "similarity"]
