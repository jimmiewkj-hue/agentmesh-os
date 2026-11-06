"""Final answer assembly: many agents' results, one readable answer.

The requirement's economics are "结果同步，不是任务同步".  A run may read a
hundred files across four agents; what comes back to the user (and to the main
node) is a **digest**: summary, findings, changed files, tests, risks,
unresolved items, and the cost of getting there.

Design constraints:

* **Always available.** Aggregation is deterministic and runs in-process. It
  does not need a model, a network, or a successful premium call. A polished
  version is an *upgrade*, never a prerequisite.
* **Never invents.** Every line comes from a task's structured result package.
  Missing data is reported as missing instead of being smoothed over.
* **Shows the money.** Per-task agent/model/tier/tokens, so "which model did
  what, and what did it cost" is answerable without reading logs.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..util import now_iso
from .roles import classify_model

_TIER_RANK = {"local": 0, "free": 1, "cheap": 2, "standard": 3, "strong": 4}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _tokens(usage: dict[str, Any]) -> dict[str, int]:
    data = usage if isinstance(usage, dict) else {}
    def num(*keys: str) -> int:
        for key in keys:
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
        return 0
    total = num("total", "total_tokens", "totalTokens")
    input_tokens = num("input", "input_tokens", "inputTokens")
    output_tokens = num("output", "output_tokens", "outputTokens")
    if not total:
        total = input_tokens + output_tokens
    return {"input": input_tokens, "output": output_tokens, "total": total}


def _section(task: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any]:
    payload = result or {}
    usage = _tokens(payload.get("token_usage") or {})
    model = str(task.get("model") or "")
    return {
        "task_id": task.get("task_id", ""),
        "title": task.get("title", ""),
        "role": task.get("role", ""),
        "type": task.get("type", ""),
        "status": task.get("status", ""),
        "verification": task.get("verification_status", ""),
        "agent_id": task.get("assigned_agent", ""),
        "model": model,
        "model_class": classify_model(model) if model else "",
        "tier": _TIER_RANK.get(classify_model(model), 3) if model else None,
        "summary": str(payload.get("summary") or ""),
        "findings": _as_list(payload.get("findings"))[:20],
        "files_changed": [str(v) for v in _as_list(payload.get("files_changed"))][:50],
        "files_read": [str(v) for v in _as_list(payload.get("files_read"))][:50],
        "tests": _as_list(payload.get("tests"))[:20],
        "evidence": _as_list(payload.get("evidence"))[:20],
        "risks": _as_list(payload.get("risks"))[:10],
        "unresolved": _as_list(payload.get("unresolved_items"))[:10],
        "next_actions": _as_list(payload.get("recommended_next_action"))[:10],
        "tokens": usage,
        "error": str(task.get("error") or ""),
    }


def _md_list(items: Iterable[Any], *, empty: str = "（无）") -> str:
    rows: list[str] = []
    for item in items:
        if isinstance(item, dict):
            parts = [f"{k}: {v}" for k, v in item.items() if v not in (None, "", [], {})]
            rows.append("- " + "；".join(str(p) for p in parts))
        else:
            text = str(item).strip()
            if text:
                rows.append(f"- {text}")
    return "\n".join(rows) if rows else empty


def build_answer(*, goal: str, run: dict[str, Any] | None, plan: dict[str, Any] | None,
                 tasks: list[dict[str, Any]], results: dict[str, dict[str, Any]],
                 generated_at: str = "") -> dict[str, Any]:
    sections = [_section(task, results.get(str(task.get("task_id")))) for task in tasks]
    total_tokens = sum(s["tokens"]["total"] for s in sections)
    by_agent: dict[str, dict[str, Any]] = {}
    by_tier: dict[str, dict[str, Any]] = {}
    for section in sections:
        agent = section["agent_id"] or "unassigned"
        bucket = by_agent.setdefault(agent, {"agent_id": agent, "tasks": 0, "tokens": 0,
                                             "models": []})
        bucket["tasks"] += 1
        bucket["tokens"] += section["tokens"]["total"]
        if section["model"] and section["model"] not in bucket["models"]:
            bucket["models"].append(section["model"])
        tier = section["tier"]
        if tier is not None:
            key = str(tier)
            tbucket = by_tier.setdefault(key, {"tier": tier, "label": section["model_class"],
                                               "tasks": 0, "tokens": 0})
            tbucket["tasks"] += 1
            tbucket["tokens"] += section["tokens"]["total"]

    completed = [s for s in sections if s["status"] == "completed"]
    failed = [s for s in sections if s["status"] in ("failed", "canceled", "rejected")]
    unresolved: list[Any] = []
    next_actions: list[Any] = []
    for section in sections:
        unresolved.extend(section["unresolved"])
        next_actions.extend(section["next_actions"])
    changed: list[str] = []
    for section in sections:
        for path in section["files_changed"]:
            if path not in changed:
                changed.append(path)

    if not plan:
        conclusion = "本次运行找不到计划记录，只能给出任务级结果。"
    elif failed:
        conclusion = (f"共 {len(sections)} 个阶段，{len(completed)} 个完成、"
                      f"{len(failed)} 个失败：" +
                      "、".join(f"{s['task_id']}({s['role'] or s['type']})" for s in failed))
    else:
        conclusion = f"共 {len(sections)} 个阶段全部完成，改动 {len(changed)} 个文件。"

    lines: list[str] = [f"# {goal}", ""]
    lines.append(f"**结论**：{conclusion}")
    run_status = (run or {}).get("status", "")
    if run_status:
        lines.append(f"**运行状态**：{run_status}"
                     + (f"（验收 {run.get('acceptance_status', '')}）"
                        if run.get("acceptance_status") else ""))
    lines.append("")
    lines.append("## 分阶段结果")
    for section in sections:
        label = section["role"] or section["type"]
        head = f"### {section['task_id']} · {label} · {section['status']}"
        lines.append(head)
        meta = [f"agent={section['agent_id'] or '-'}",
                f"model={section['model'] or '-'}"]
        if section["tier"] is not None:
            meta.append(f"tier={section['tier']}({section['model_class']})")
        if section["verification"]:
            meta.append(f"验证={section['verification']}")
        if section["tokens"]["total"]:
            meta.append(f"tokens={section['tokens']['total']}")
        lines.append("`" + " | ".join(meta) + "`")
        lines.append("")
        lines.append(section["summary"] or (section["error"] or "（无摘要）"))
        if section["findings"]:
            lines.append("")
            lines.append("**发现**")
            lines.append(_md_list(section["findings"]))
        if section["tests"]:
            lines.append("")
            lines.append("**测试**")
            lines.append(_md_list(section["tests"]))
        if section["error"]:
            lines.append("")
            lines.append(f"**错误**：{section['error'][:500]}")
        lines.append("")

    if changed:
        lines.append("## 变更文件")
        lines.append(_md_list(changed))
        lines.append("")
    if unresolved:
        lines.append("## 未解决项")
        lines.append(_md_list(unresolved))
        lines.append("")
    if next_actions:
        lines.append("## 建议下一步")
        lines.append(_md_list(next_actions))
        lines.append("")

    lines.append("## 成本与模型分布")
    if total_tokens:
        lines.append(f"- 总 token：{total_tokens}")
    else:
        lines.append("- 总 token：未上报（该 Agent 未回传用量，控制平面不做估算填充）")
    lines.append(f"- 阶段数：{len(sections)}（premium 档 "
                 f"{sum(1 for s in sections if (s['tier'] or 0) >= 3)} 个）")
    for agent in sorted(by_agent.values(), key=lambda b: (-b["tokens"], b["agent_id"])):
        lines.append(f"- {agent['agent_id']}：{agent['tasks']} 个任务 / {agent['tokens']} tokens"
                     + (f" / {', '.join(agent['models'])}" if agent["models"] else ""))
    for tier in sorted(by_tier):
        bucket = by_tier[tier]
        lines.append(f"- tier {tier}（{bucket['label']}）：{bucket['tasks']} 个任务 / "
                     f"{bucket['tokens']} tokens")
    lines.append("")
    lines.append(f"_生成时间：{generated_at or now_iso()}_")

    return {
        "goal": goal,
        "run_id": (run or {}).get("run_id", ""),
        "run_status": run_status,
        "acceptance_status": (run or {}).get("acceptance_status", ""),
        "generated_at": generated_at or now_iso(),
        "conclusion": conclusion,
        "sections": sections,
        "files_changed": changed,
        "unresolved": unresolved,
        "next_actions": next_actions,
        "cost": {
            "total_tokens": total_tokens,
            "by_agent": sorted(by_agent.values(), key=lambda b: (-b["tokens"], b["agent_id"])),
            "by_tier": [by_tier[k] for k in sorted(by_tier)],
            "premium_tasks": sum(1 for s in sections if (s["tier"] or 0) >= 3),
        },
        "answer_markdown": "\n".join(lines).strip() + "\n",
        "polished": False,
    }


POLISH_TEMPLATE_HEADER = """你是本次运行的汇总节点。下面已经有一份由控制平面根据各任务成果包生成的
确定性汇总（不是模型写的，可能有表述生硬，但事实与数字可信）。

要求：
- 只做“压缩与重述”，不得新增未出现的事实、数字、文件名或测试结论；
- 保留所有「未解决项」与「风险」，不得为了让结论好看而省略；
- 输出 Markdown，结构为：结论（3 行内）→ 关键改动 → 验证情况 → 未解决项 → 下一步；
- 不要重复每个阶段的元数据（agent/model/tokens），那部分保留在原文里。

===== 确定性汇总原文 =====
"""


def build_polish_prompt(answer: dict[str, Any]) -> str:
    """Prompt for the optional summarizer role; kept here so it is versioned
    together with the deterministic output it rewrites."""
    return POLISH_TEMPLATE_HEADER + str(answer.get("answer_markdown") or "")


def apply_polish(answer: dict[str, Any], text: str) -> dict[str, Any]:
    """Merge a model-written summary back, keeping the deterministic body."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return answer
    updated = dict(answer)
    updated["polished"] = True
    updated["polish_text"] = cleaned
    updated["answer_markdown"] = (
        f"# {answer.get('goal', '')}\n\n"
        f"{cleaned}\n\n"
        "---\n\n"
        "## 附录：确定性汇总（未经模型改写）\n\n"
        + str(answer.get("answer_markdown") or "")
    )
    return updated


__all__ = ["build_answer", "build_polish_prompt", "apply_polish"]
