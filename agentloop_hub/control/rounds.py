"""Multi-round continuation policy.

Two reference implementations shaped this policy:

* opencode's session runner continues while a step produced local tool calls or
  pending input, injects a synthetic message when a subagent finishes, and
  force-closes with a max-steps prompt.  It has no semantic "goal achieved"
  check; the model ends its turn by not calling tools.
* DSH persists one goal per session and a goal-round driver starts another
  model turn only when the agent is idle, continuation is armed and rounds
  remain.  The model declares ``complete`` / ``blocked``; a blocker must repeat
  for several rounds before it is accepted, and the round cap records a
  ``round-limit`` blocker.

AgentMesh executes a task graph, not a chat turn, so the equivalent of
"no more tool calls" is "the main node's summary is accepted and every task is
complete".  A rejected or failed round becomes the input to the next planning
round instead of ending the run.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..models import Task, TaskPlan, TaskSpec, TaskStatus, VerificationStatus

# 2026-11-07 defect #7: the fallback planner told the agent to "git rm
# --cached 并提交" while the plan forbade commits, and --cached produces no
# patch diff, so verification failed on an empty patch.  These delivery rules
# are restated in every fallback/replan task so recorded suggestions can never
# override the control plane's actual delivery mechanism.
DELIVERY_RULES = (
    "成果只能以工作区文件变更（patch）交付；禁止 git commit/push。",
    "`git rm --cached` 不产生补丁差异，禁止作为修复手段；"
    "需要版本库停止跟踪某文件时写入 unresolved，由人类在合并阶段处理（若 scope 允许可修改 .gitignore）。",
    "未解决项中的建议若与计划约束或上述规则冲突，以约束为准，并在成果包中说明冲突。",
)

FAILED_STATES = {
    TaskStatus.FAILED.value,
    TaskStatus.BLOCKED.value,
    TaskStatus.REJECTED.value,
}
DONE_STATES = {TaskStatus.COMPLETED.value}


@dataclass
class RoundDecision:
    action: str  # complete | continue | blocked
    reason: str
    code: str = ""
    goal_status: str = "active"
    unresolved: list[str] = field(default_factory=list)
    signature: str = ""
    blocked_streak: int = 0
    round_number: int = 1
    max_rounds: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "code": self.code,
            "goal_status": self.goal_status,
            "unresolved": self.unresolved,
            "signature": self.signature,
            "blocked_streak": self.blocked_streak,
            "round_number": self.round_number,
            "max_rounds": self.max_rounds,
        }


def _clean(value: Any, limit: int = 600) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _dedupe(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = _clean(item)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def collect_unresolved(tasks: list[Task], summary: dict[str, Any],
                       results: dict[str, dict[str, Any]] | None = None) -> list[str]:
    """Concrete reasons the previous round cannot be called complete.

    Results are keyed by task uid; only the documented ``unresolved_items``
    field is consumed.  A reject without next actions still yields a stable
    signature, so repeated identical rejections can be recognised.
    """
    items: list[str] = []
    by_uid = {task.uid: task for task in tasks}
    # A later completed task explicitly closes the failures it supersedes.
    # History is retained; only the delivery gate treats them as closed.
    closed_by: dict[str, str] = {}
    for task in tasks:
        if task.status in DONE_STATES:
            for superseded in getattr(task.spec, "supersedes", []) or []:
                closed_by[str(superseded)] = task.task_id
    for task in tasks:
        if task.task_id in closed_by:
            continue
        if task.status in FAILED_STATES:
            items.append(f"任务 {task.task_id} 状态 {task.status}: {task.error or '未说明'}")
        if task.verification_status == VerificationStatus.FAILED.value:
            latest = (results or {}).get(task.uid) or {}
            tests = latest.get("tests") or []
            detail = "; ".join(
                str(t.get("command") or t.get("test") or "")
                for t in tests if isinstance(t, dict) and str(t.get("result")) == "failed"
            )
            items.append(
                f"任务 {task.task_id} 独立验证失败"
                + (f": {detail}" if detail else (f": {task.error}" if task.error else ""))
            )
    for uid, payload in (results or {}).items():
        task = by_uid.get(uid)
        label = task.task_id if task else uid
        for raw in payload.get("unresolved_items") or []:
            if isinstance(raw, dict):
                if raw.get("blocking") is False:
                    continue
                if str(raw.get("severity") or "").lower() in (
                        "info", "low", "suggestion", "optional"):
                    continue
                text = raw.get("summary") or raw.get("description") or raw.get("finding") or raw
            else:
                text = raw
            text = str(text or "").strip()
            if not text:
                continue
            lowered = text.lower()
            if any(marker in lowered for marker in
                   ("optional", "non-blocking", "nonblocking", "建议", "可选", "非阻塞")):
                continue
            items.append(f"{label} 未解决: {text}")
    recommendation = str(summary.get("merge_recommendation") or "").lower()
    declared = str(summary.get("goal_status") or "").lower()
    if recommendation and recommendation != "approve":
        actions = [str(x) for x in (summary.get("next_actions") or []) if str(x).strip()]
        risks = [str(x) for x in (summary.get("risks") or []) if str(x).strip()]
        if actions:
            items.extend(f"主节点下一步: {x}" for x in actions)
        elif risks:
            items.extend(f"主节点风险: {x}" for x in risks)
        else:
            items.append(
                "主节点判定未通过: "
                + _clean(summary.get("completion") or "未说明未通过原因")
            )
    if declared == "blocked":
        reason = summary.get("blocking_reason")
        items.append("主节点声明阻塞: " + _clean(reason or summary.get("completion") or "未说明原因"))
    return _dedupe(items)


def round_tasks_for(tasks: list[Task], round_number: int) -> list[Task]:
    """Tasks introduced by this continuation round.

    Earlier rounds stay in history and may still carry failed rows.  A repair
    round is judged on its own new tasks plus the main node's global summary;
    otherwise one failed first-round row would make every later round unable
    to complete.  Round 1 has no prefix, so it uses the whole graph.
    """
    prefix = f"R{int(round_number)}_"
    current = [task for task in tasks if task.task_id.startswith(prefix)]
    return current or tasks


def signature_of(items: Iterable[str]) -> str:
    joined = "\n".join(sorted(_dedupe(items)))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def previous_round_state(history: list[dict[str, Any]]) -> tuple[str, int]:
    """Return (signature, streak) of the newest recorded round."""
    if not history:
        return "", 0
    last = history[-1] if isinstance(history[-1], dict) else {}
    return str(last.get("signature") or ""), int(last.get("blocked_streak") or 0)


def decide(*, tasks: list[Task], summary: dict[str, Any], round_number: int,
           max_rounds: int, blocked_after_rounds: int,
           history: list[dict[str, Any]] | None = None,
           results: dict[str, dict[str, Any]] | None = None) -> RoundDecision:
    """Classify a finished round as complete, continuing, or blocked.

    Completion is intentionally conservative: the main node must recommend
    approval *and* every task must be completed *and* no deterministic
    verification may have failed.  A model-complete claim cannot override a
    failed task.  This mirrors DSH's rule that only the model declares
    completion, while the harness keeps deterministic safety checks.
    """
    history = history or []
    unresolved = collect_unresolved(tasks, summary, results)
    signature = signature_of(unresolved)
    previous_signature, previous_streak = previous_round_state(history)
    streak = previous_streak + 1 if signature and signature == previous_signature else 1

    current = round_tasks_for(tasks, round_number)
    all_completed = bool(current) and all(t.status in DONE_STATES for t in current)
    has_failure = any(t.status in FAILED_STATES for t in current)
    verification_failed = any(
        t.verification_status == VerificationStatus.FAILED.value for t in current
    )
    recommendation = str(summary.get("merge_recommendation") or "").lower()
    declared = str(summary.get("goal_status") or "").lower()

    if (all_completed and not has_failure and not verification_failed
            and not unresolved
            and recommendation == "approve" and declared not in ("blocked", "continue")):
        return RoundDecision(
            action="complete", reason="主节点批准且全部任务完成、无验证失败、无未关闭必需项",
            goal_status="complete", unresolved=[], signature=signature,
            blocked_streak=0, round_number=round_number, max_rounds=max_rounds,
        )

    if declared == "blocked" and str(summary.get("blocking_reason") or "").strip():
        if streak >= max(1, int(blocked_after_rounds)):
            return RoundDecision(
                action="blocked", reason="主节点声明的阻塞条件连续出现",
                code="model-blocked", goal_status="blocked", unresolved=unresolved,
                signature=signature, blocked_streak=streak,
                round_number=round_number, max_rounds=max_rounds,
            )

    if round_number >= max(1, int(max_rounds)):
        return RoundDecision(
            action="blocked",
            reason=f"已达到最大轮次 {max_rounds}，目标仍未完成",
            code="round-limit", goal_status="blocked", unresolved=unresolved,
            signature=signature, blocked_streak=streak,
            round_number=round_number, max_rounds=max_rounds,
        )

    if (previous_signature and signature == previous_signature
            and streak >= max(1, int(blocked_after_rounds))):
        return RoundDecision(
            action="blocked", reason="同一阻塞条件连续出现，继续自动重试不会改变结果",
            code="repeated-blocker", goal_status="blocked", unresolved=unresolved,
            signature=signature, blocked_streak=streak,
            round_number=round_number, max_rounds=max_rounds,
        )

    reason = (
        "主节点要求继续" if recommendation != "approve"
        else "任务或验证仍未全部通过"
    )
    return RoundDecision(
        action="continue", reason=reason, goal_status="active", unresolved=unresolved,
        signature=signature, blocked_streak=streak,
        round_number=round_number, max_rounds=max_rounds,
    )


def _prior_dependencies(tasks: list[Task]) -> list[str]:
    writable = [t.task_id for t in tasks
                if t.status == TaskStatus.COMPLETED.value and t.spec.writable]
    if writable:
        return list(dict.fromkeys(writable))
    return list(dict.fromkeys(
        t.task_id for t in tasks if t.status == TaskStatus.COMPLETED.value
    ))


def _fallback_scope(tasks: list[Task]) -> list[str]:
    scopes: list[str] = []
    for task in tasks:
        if task.status not in FAILED_STATES and not task.spec.writable:
            continue
        for scope in task.spec.scope:
            if scope and scope not in scopes:
                scopes.append(scope)
    return scopes[:20] or ["**"]


def _fallback_commands(tasks: list[Task], test_command: str) -> list[str]:
    commands: list[str] = []
    for task in tasks:
        for command in task.spec.verification_commands:
            if command and command not in commands:
                commands.append(command)
    if not commands and test_command:
        commands.append(test_command)
    return commands[:8]


def fallback_specs(*, plan: TaskPlan, tasks: list[Task], summary: dict[str, Any],
                   decision: RoundDecision, round_number: int,
                   default_token_budget: int = 30000,
                   default_time_limit_minutes: int = 30,
                   default_retry_limit: int = 2,
                   test_command: str = "") -> list[TaskSpec]:
    """Build the smallest evidence-based repair round when replanning fails.

    The task text is derived from recorded failures, verification reports and
    the main node's next actions; no new requirements are invented.
    """
    prior = [t for t in tasks if t.status == TaskStatus.COMPLETED.value]
    writable = [t for t in prior if t.spec.writable]
    source = next(
        (t for t in reversed(writable)
         if t.assigned_agent or t.spec.suggested_agent or t.spec.model),
        None,
    )
    owner = ""
    model = ""
    if source:
        owner = source.assigned_agent or source.spec.suggested_agent
        model = source.spec.model
    deps = _prior_dependencies(tasks)
    unresolved = decision.unresolved or ["上一轮未通过验收，但未记录具体原因"]
    bullet = "\n".join(f"- {item}" for item in unresolved[:12])
    constraints = [str(item).strip() for item in (plan.constraints or [])
                   if str(item).strip()]
    constraint_block = ""
    if constraints:
        constraint_block = (
            "任务约束（强制，逐条遵守）：\n"
            + "\n".join(f"- {item}" for item in constraints) + "\n")
    delivery_block = (
        "交付机制（强制）：\n"
        + "\n".join(f"- {item}" for item in DELIVERY_RULES) + "\n")
    impl_id = f"R{round_number}_T001"
    commands = _fallback_commands(tasks, test_command)
    implementation = TaskSpec(
        task_id=impl_id,
        title=f"第 {round_number} 轮修复未解决项",
        type="implementation",
        role="implement",
        model=model,
        suggested_agent=owner,
        objective=(
            f"第 {round_number} 轮续跑：原始目标“{plan.goal}”上一轮未通过。"
            f"上一轮结论：{summary.get('completion') or '未说明'}。\n"
            f"{constraint_block}{delivery_block}"
            f"请先读取当前工作区（已包含上一轮依赖任务补丁），只修复以下未解决项，"
            f"不要重复已完成工作；必须给出文件定位与命令/测试证据。\n{bullet}"
        ),
        dependencies=deps,
        scope=_fallback_scope(tasks),
        out_of_scope=[],
        supersedes=[t.task_id for t in tasks if t.status in FAILED_STATES],
        expected_outputs=["code_patch", "summary"],
        acceptance_criteria=[
            "逐项关闭上一轮未解决项",
            "修改有证据（文件定位）且验证命令通过或说明无法执行的原因",
        ],
        verification_commands=commands,
        token_budget=default_token_budget,
        time_limit_minutes=default_time_limit_minutes,
        retry_limit=default_retry_limit,
        risk_level="medium",
        priority=8,
    )
    review = TaskSpec(
        task_id=f"R{round_number}_REVIEW",
        title=f"第 {round_number} 轮主节点复审",
        type="review",
        role="review",
        model=plan.model,
        suggested_agent=plan.main_agent_id,
        objective=(
            "只读复审本轮修复。逐项核对上一轮未解决项是否真正关闭；必须实际读取相关文件"
            "并检查测试/命令证据，不能采信执行 Agent 的自述。仍有问题时按严重程度列出"
            "定位与可执行修复建议。"
        ),
        dependencies=[impl_id],
        scope=[],
        expected_outputs=["summary"],
        acceptance_criteria=[
            "逐项给出关闭/未关闭结论",
            "结论含文件定位与实际验证证据",
        ],
        token_budget=max(15000, default_token_budget // 2),
        time_limit_minutes=20,
        retry_limit=1,
        risk_level="low",
        priority=2,
    )
    implementation.validate()
    review.validate()
    return [implementation, review]


def prefix_delta_specs(*, specs: list[TaskSpec], round_number: int, tasks: list[Task],
                       plan: TaskPlan) -> list[TaskSpec] | None:
    """Rename a planner's delta graph and attach prior-round dependencies.

    The main node only plans the next minimal delta; IDs are namespaced per
    round so a plan can be extended in place.  Root tasks depend on completed
    writable tasks, which makes the existing patch-composition path apply the
    previous round's code before the new task starts.
    """
    prior = _prior_dependencies(tasks)
    mapping = {spec.task_id: f"R{round_number}_{spec.task_id}" for spec in specs}
    out: list[TaskSpec] = []
    for spec in specs:
        copy = TaskSpec.from_dict(spec.to_dict())
        copy.task_id = mapping[spec.task_id]
        copy.parent_task_id = mapping.get(copy.parent_task_id, copy.parent_task_id)
        copy.dependencies = [mapping.get(dep, dep) for dep in copy.dependencies]
        if not copy.dependencies and prior:
            copy.dependencies = [dep for dep in prior if dep != copy.task_id]
        copy.validate()
        out.append(copy)
    if not any(spec.writable for spec in out):
        return None
    # Revalidate the combined graph; dependencies may only point at tasks that
    # exist in the extended plan (all prior IDs are already present).
    ids = {spec.task_id for spec in plan.tasks} | {spec.task_id for spec in out}
    for spec in out:
        missing = [dep for dep in spec.dependencies if dep not in ids]
        if missing:
            return None
    return out


__all__ = [
    "RoundDecision", "collect_unresolved", "signature_of", "previous_round_state",
    "decide", "fallback_specs", "prefix_delta_specs",
]
