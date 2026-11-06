"""Collaboration contract: pin planning roles to the user's selected agents.

The WebUI already lets the user pick one main agent and the real model for
each sub agent.  A model that was explicitly selected is the user's opt-in for
that agent, so the controller uses it to build a deterministic role contract:

* plan / review / summarize stay on the main node;
* explore / research / implement / write / verify go to the selected sub agent(s).

The LLM planner is still responsible for the task graph; this module only
enforces ownership, role labels and the concrete model so the router cannot
silently pick an agent the user did not enable.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..models import AgentRecord, TaskPlan, TaskSpec

SUB_ROLES = ("explore", "research", "implement", "write", "verify")
MAIN_ROLES = ("plan", "review", "summarize")
ALL_ROLES = MAIN_ROLES + SUB_ROLES

_RESEARCH_HINTS = (
    "资料", "史实", "参考", "调研", "研究", "来源", "许可", "外部", "官网",
    "文献", "版本核对", "公开授权", "research", "source", "license",
)

# LLM planners sometimes use a role name (research/implement/write/verify)
# where the schema expects a TaskType.  Normalising here keeps a correct role
# graph from being rejected for a naming slip.
TYPE_BY_ROLE = {
    "explore": "analysis",
    "research": "analysis",
    "plan": "analysis",
    "summarize": "analysis",
    "implement": "implementation",
    "write": "documentation",
    "verify": "verification",
    "review": "review",
}
VALID_TYPES = {"analysis", "review", "implementation", "verification",
               "documentation", "architecture"}
ROLE_TYPE = {
    "explore": "analysis",
    "research": "analysis",
    "plan": "analysis",
    "summarize": "analysis",
    "implement": "implementation",
    "write": "documentation",
    "verify": "verification",
    "review": "review",
}
VALID_APPROVAL = {"auto", "required", "plan_only"}
VALID_RISK = {"low", "medium", "high", "critical"}
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


def _canonical_role(task: dict[str, Any], kind: str) -> str:
    """Map planner labels (often Chinese) onto a legal role identifier."""
    raw = str(task.get("role") or "").strip()
    if raw and _ROLE_RE.match(raw):
        return raw.lower()
    text = f"{task.get('title') or ''} {task.get('objective') or ''}".lower()
    if kind == "review":
        return "review"
    if kind == "verification":
        return "verify"
    if kind == "documentation":
        return "write"
    if kind == "implementation":
        return "implement"
    if kind == "architecture":
        return "plan"
    if any(hint.lower() in text for hint in _RESEARCH_HINTS):
        return "research"
    return "explore"


def normalize_plan_payload(raw: Any) -> Any:
    """Map role words in ``type`` onto legal TaskType values, in place."""
    if not isinstance(raw, dict):
        return raw
    for key in ("constraints", "requirements", "nonfunctional",
                "open_questions", "risks"):
        value = raw.get(key)
        if not isinstance(value, list):
            continue
        normalized: list[str] = []
        for item in value:
            if isinstance(item, str):
                normalized.append(item)
            elif isinstance(item, dict):
                risk = str(item.get("risk") or item.get("description") or "").strip()
                mitigation = str(item.get("mitigation") or "").strip()
                if key == "risks" and (risk or mitigation):
                    normalized.append(
                        f"{risk} — {mitigation}".strip(" —"))
                else:
                    normalized.append(json.dumps(item, ensure_ascii=False))
            elif item is not None:
                normalized.append(str(item))
        raw[key] = normalized
    tasks = raw.get("tasks")
    if not isinstance(tasks, list):
        return raw
    for task in tasks:
        if not isinstance(task, dict):
            continue
        kind = str(task.get("type") or "").strip().lower()
        role = str(task.get("role") or "").strip().lower()
        if kind not in VALID_TYPES:
            mapped = TYPE_BY_ROLE.get(kind) or TYPE_BY_ROLE.get(role)
            if mapped:
                task["type"] = mapped
                kind = mapped
        if not task.get("role") and kind in TYPE_BY_ROLE:
            task["role"] = kind
        else:
            task["role"] = _canonical_role(task, kind)
        approval = str(task.get("approval_policy") or "auto").strip().lower()
        task["approval_policy"] = approval if approval in VALID_APPROVAL else "required"
        risk = str(task.get("risk_level") or "low").strip().lower()
        task["risk_level"] = risk if risk in VALID_RISK else "medium"
    return raw


def _selected_subagents(agents: list[AgentRecord], main_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for agent in agents:
        if agent.agent_id == main_id or agent.status == "offline":
            continue
        model = str((agent.extra or {}).get("selected_model") or "")
        if not model:
            continue
        out.append({"agent_id": agent.agent_id, "name": agent.name, "model": model})
    return out


def build_contract(store: Any, project: Any, main_agent_id: str,
                   main_model: str = "") -> dict[str, Any]:
    agents = store.list_agents()
    main = next((a for a in agents if a.agent_id == main_agent_id), None)
    subs = _selected_subagents(agents, main_agent_id)
    return {
        "main_agent_id": main_agent_id,
        "main_name": main.name if main else main_agent_id,
        "main_model": main_model or str((main.extra or {}).get("selected_model") or "") if main else main_model,
        "sub_agents": subs,
        "roles": {
            "plan": main_agent_id, "review": main_agent_id,
            "summarize": main_agent_id,
            **{role: (subs[0]["agent_id"] if subs else "") for role in SUB_ROLES},
        },
    }


def infer_role(task: TaskSpec) -> str:
    if task.role in ALL_ROLES:
        return task.role
    if task.type == "review":
        return "review"
    if task.type == "verification":
        return "verify"
    if task.type == "documentation":
        return "write"
    if task.type == "implementation":
        return "implement"
    text = f"{task.title} {task.objective}"
    if any(hint.lower() in text.lower() for hint in _RESEARCH_HINTS):
        return "research"
    return "explore"


def _model_for(contract: dict[str, Any], agent_id: str) -> str:
    if agent_id == contract.get("main_agent_id"):
        return str(contract.get("main_model") or "")
    for item in contract.get("sub_agents") or []:
        if item.get("agent_id") == agent_id:
            return str(item.get("model") or "")
    return ""


def enforce_plan_contract(plan: TaskPlan, contract: dict[str, Any]) -> list[str]:
    """Rewrite role / suggested_agent / model; return human-readable notes."""
    sub_ids = [str(item["agent_id"]) for item in contract.get("sub_agents") or []]
    main_id = str(contract.get("main_agent_id") or "")
    if not main_id or not sub_ids:
        return ["未配置主节点或已选配模型的子 Agent，未强制角色归属"]

    notes: list[str] = [
        f"协作契约：主节点 {main_id} 负责 plan/review/summarize；"
        f"执行子 Agent 仅 {', '.join(sub_ids)}"
    ]
    changed: list[str] = []
    for task in plan.tasks:
        role = infer_role(task)
        # The planner's TaskType controls writability and scope checks.  Keep
        # it for sub roles (a research task may legitimately produce a
        # reference document); only the main node is restricted to read-only
        # review/analysis so it cannot bypass "plan/review/summarize".
        if role in MAIN_ROLES and task.type not in ("review", "analysis"):
            new_type = "review" if role == "review" else "analysis"
            changed.append(f"{task.task_id}:type {task.type}->{new_type}")
            task.type = new_type
        if role in MAIN_ROLES:
            owner = main_id
        elif task.suggested_agent in sub_ids:
            owner = task.suggested_agent
        else:
            owner = sub_ids[0]
        model = _model_for(contract, owner)
        before = (task.role, task.suggested_agent, task.model)
        task.role = role
        task.suggested_agent = owner
        if model:
            task.model = model
        after = (task.role, task.suggested_agent, task.model)
        if before != after:
            changed.append(f"{task.task_id}:{role}->{owner}")
    if changed:
        notes.append("协作契约已钉住：" + ", ".join(changed[:12]))

    has_review = any(infer_role(t) in ("review", "plan") for t in plan.tasks)
    if not has_review:
        leaf = [t.task_id for t in plan.tasks
                if not any(t.task_id in other.dependencies for other in plan.tasks)]
        used = {t.task_id for t in plan.tasks}
        task_id = "REVIEW1"
        index = 1
        while task_id in used:
            index += 1
            task_id = f"REVIEW{index}"
        plan.tasks.append(TaskSpec(
            task_id=task_id,
            title="主节点审阅",
            type="review",
            role="review",
            model=_model_for(contract, main_id),
            suggested_agent=main_id,
            objective=("主节点只读审阅实现与文档：按严重程度列出缺陷、风险、"
                       "定位与修复建议；不要修改文件。"),
            dependencies=leaf,
            expected_outputs=["summary"],
            acceptance_criteria=["问题按严重程度排序", "每条含定位与建议"],
            token_budget=25000,
            time_limit_minutes=20,
            retry_limit=1,
            priority=2,
        ))
        notes.append(f"计划没有 review 任务，已补充 {task_id} 并由主节点执行")

    plan.constraints = list(plan.constraints) + notes
    return notes


__all__ = ["SUB_ROLES", "MAIN_ROLES", "ALL_ROLES", "build_contract",
           "infer_role", "enforce_plan_contract"]
