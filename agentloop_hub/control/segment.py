"""Deterministic task segmentation: one request in, a small task graph out.

Why not just ask a model to plan?  Because planning is the *cheapest* place to
be deterministic, and the most expensive place to be wrong:

* a planner call on a premium model costs points before any work happens;
* an invalid ``plan.json`` costs a repair round-trip;
* and a plan that is right 80% of the time silently loses 20% of requests.

So the default path is rule-based and explainable. It reads *attributes* of the
request — read-only vs write, code vs document, which files were named, whether
architecture is involved — and maps them onto the roles defined by the role
policy. The LLM planner (``PlanningService``) remains available for goals the
rules cannot classify confidently; ``Segmenter`` reports that confidence rather
than guessing silently.

Rules are intentionally conservative:

* never more than ``max_tasks`` (default 4) — over-segmentation costs more than
  it saves, which the DSH policy states explicitly ("不重复启动作用相同的子 Agent");
* a write task always gets an explicit scope when the user named files;
* a verification task is only added when there is something to verify
  (a test command exists);
* a pure question becomes a read-only explore, never a write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..models import TaskSpec
from .roles import RolePolicy


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

_PATH_RE = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|pyi|js|jsx|ts|tsx|md|rst|json|ya?ml|"
    r"toml|ini|cfg|txt|html|css|scss|go|rs|java|kt|kts|cs|c|h|cpp|hpp|sh|ps1|sql|"
    r"proto|vue|svelte))",
    re.IGNORECASE,
)

_DIR_RE = re.compile(r"(?<![\w/.-])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+)(?![\w/.-])")

_INTENT_PATTERNS: dict[str, tuple[str, ...]] = {
    "design": (
        "架构", "设计", "技术方案", "设计方案", "模块划分", "技术选型", "重构方案", "拆解",
        "architecture", "design", "adr", "migration plan", "module boundary",
    ),
    "implement": (
        "修复", "修一下", "改一下", "实现", "新增", "添加", "增加", "加个", "补充",
        "写个", "写一个", "重构", "优化", "迁移",
        "fix", "implement", "add ", "refactor", "migrate", "write code",
        "改成", "替换",
    ),
    "review": (
        "审查", "评审", "审阅", "代码检查", "找 bug", "找bug", "定位问题", "review", "audit",
        "code review", "检查一下代码",
    ),
    "docs": (
        "文档", "报告", "说明书", "使用说明", "readme", "doc", "documentation",
        "report", "写一份", "撰写",
    ),
    "explore": (
        "扫描", "梳理", "分析", "看看", "了解一下", "了解", "摸清", "排查", "检索",
        "解释", "说明一下",
        "scan", "analy", "explore", "understand", "inspect", "locate", "trace", "explain",
    ),
    "test": (
        "测试", "单测", "单元测试", "test", "pytest", "coverage", "回归",
    ),
    "verify": (
        "验证", "验收", "复核", "verify", "validate", "check that",
    ),
}

# Questions that should never turn into a write task.
_QUESTION_HINTS = (
    "为什么", "怎么", "如何", "是什么", "能不能", "是否", "解释", "why", "how",
    "what", "explain", "is it",
)

# An explicit imperative overrides a question word: "为什么 add() 返回减法？帮我修复"
# is a work request, not a question.
_IMPERATIVE_HINTS = (
    "修复", "修一下", "改一下", "实现", "新增", "添加", "增加", "补充", "写个",
    "重构", "改成", "替换",
    "fix", "implement", "refactor",
)

_ROLE_TITLES: dict[str, str] = {
    "explore": "探索与定位",
    "research": "外部调研",
    "implement": "实现与修改",
    "review": "审阅与问题定位",
    "plan": "方案设计",
    "write": "文档撰写",
    "verify": "独立验证",
}

_ROLE_OBJECTIVES: dict[str, str] = {
    "explore": "只读扫描与定位：找出与本需求相关的文件、符号与依赖，给出结论与证据；不要修改任何文件。",
    "research": "外部调研：核实官方文档/API/issue 的准确用法，标明来源与版本；不要修改项目文件。",
    "implement": "在 scope 内完成修改，补齐必要测试并运行可执行的验证命令，产出结构化成果包与可定位证据。",
    "review": "只读审阅：按严重程度列出缺陷与风险，给出文件/行号定位与修改建议；不要修改文件。",
    "plan": "只读规划：给出推荐方案、备选方案、实施顺序、风险与回滚方式、验收标准；不要修改文件。",
    "write": "在 scope 内撰写/更新文档，保持与代码事实一致，不确定处标注待确认。",
    "verify": "独立验证：复跑验证命令，检查改动范围与证据是否成立；不得直接采信执行方结论。",
}

_ROLE_ACCEPTANCE: dict[str, list[str]] = {
    "explore": ["列出相关文件与符号", "每条结论带可定位证据"],
    "research": ["标明来源与版本", "区分官方事实与推断"],
    "implement": ["修改落在 scope 内", "验证命令通过或说明原因", "证据可定位"],
    "review": ["问题按严重程度排序", "每条含定位与建议"],
    "plan": ["给出推荐方案与否决理由", "包含风险与回滚", "给出验收标准"],
    "write": ["文档与代码事实一致", "结构完整可读"],
    "verify": ["复跑命令并记录结果", "范围检查结论明确"],
}

_ROLE_BUDGET: dict[str, tuple[int, int]] = {
    # role -> (token_budget, time_limit_minutes)
    "explore": (20000, 15),
    "research": (20000, 15),
    "implement": (50000, 30),
    "review": (20000, 15),
    "plan": (25000, 20),
    "write": (30000, 20),
    "verify": (20000, 15),
}


@dataclass
class Segment:
    goal: str
    tasks: list[TaskSpec]
    signals: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "confidence": round(float(self.confidence), 4),
            "signals": self.signals,
            "notes": list(self.notes),
            "tasks": [t.to_dict() for t in self.tasks],
        }


class Segmenter:
    """Rule-based request splitter bound to a role policy."""

    def __init__(self, policy: RolePolicy, *, max_tasks: int = 4,
                 test_command: str = ""):
        self.policy = policy
        self.max_tasks = max(1, int(max_tasks))
        self.test_command = str(test_command or "").strip()

    # -- public ---------------------------------------------------------
    def segment(self, goal: str) -> Segment:
        text = str(goal or "").strip()
        if not text:
            raise ValueError("需求不能为空")
        signals = self.analyze(text)
        plan = self._choose_roles(signals)
        tasks = self._build_tasks(text, plan, signals)
        confidence = self._confidence(signals, plan)
        notes = list(signals["reasons"])
        if signals.get("truncated"):
            notes.append(f"任务数收敛到 {self.max_tasks}（避免过度切分）")
        if signals["paths"]:
            notes.append(f"从需求中识别到文件：{', '.join(signals['paths'][:8])}")
        else:
            notes.append("需求未点名文件，写任务按全仓范围执行")
        return Segment(goal=text, tasks=tasks, signals=signals,
                       confidence=confidence, notes=notes)

    def analyze(self, text: str) -> dict[str, Any]:
        low = text.lower()
        hits: dict[str, list[str]] = {}
        for intent, keys in _INTENT_PATTERNS.items():
            found = [k for k in keys if k.lower() in low]
            if found:
                hits[intent] = found
        paths: list[str] = []
        for match in _PATH_RE.findall(text):
            rel = match.replace("\\", "/").lstrip("./")
            if rel and rel not in paths:
                paths.append(rel)
        # Directory hints ("src/api", "agentloop_hub/control") — ASCII tokens with
        # a slash and no file extension become directory scopes.
        for match in _DIR_RE.findall(text):
            rel = match.strip("/").replace("\\", "/")
            if not rel or rel.lower().startswith(("http:", "https:")):
                continue
            if re.match(r"^[A-Za-z]:", rel):
                continue  # absolute Windows path: never a repo scope
            if _PATH_RE.fullmatch(rel):
                continue
            if any(existing.startswith(rel + "/") for existing in paths):
                continue
            scoped = rel + "/**"
            if scoped not in paths:
                paths.append(scoped)
        imperative = any(h in low for h in _IMPERATIVE_HINTS)
        question = any(h in low for h in _QUESTION_HINTS) and not imperative
        reasons = [
            f"命中意图 {name}: {', '.join(values)}" for name, values in hits.items()
        ]
        if question:
            reasons.append("识别为提问：只读回答，不产生写任务")
        return {
            "intents": hits, "paths": paths, "question": question,
            "wants_write": bool({"implement", "docs"} & set(hits)) and not question,
            "reasons": reasons,
            "length": len(text),
        }

    # -- planning -------------------------------------------------------
    def _choose_roles(self, signals: dict[str, Any]) -> list[str]:
        intents = set(signals["intents"])
        roles: list[str] = []
        if "design" in intents:
            roles.append("plan")
        if signals["wants_write"]:
            if "implement" in intents:
                if not signals["paths"] and "design" not in intents:
                    # Files unknown: locate them first, cheaply, before writing.
                    roles.append("explore")
                roles.append("implement")
            if "docs" in intents and "implement" not in intents:
                roles.append("write")
            elif "docs" in intents:
                # Implement + document in one request: document after the code.
                roles.append("write")
            if "review" in intents:
                roles.append("review")
            if self._needs_verify(intents):
                roles.append("verify")
        elif "review" in intents:
            roles.append("review")
        elif "docs" in intents:
            roles.append("write")
        elif "explore" in intents or "research" in intents:
            roles.append("research" if "research" in intents else "explore")
        else:
            roles.append("explore")
        # Keep only roles the policy actually knows, preserve order, cap length.
        out: list[str] = []
        for role in roles:
            if role in self.policy.roles and role not in out:
                out.append(role)
        if not out:
            out = ["explore"]
        if len(out) > self.max_tasks:
            signals["truncated"] = True
            # Always keep the write step; drop the cheapest auxiliary role.
            keep = out[-self.max_tasks:]
            out = keep
        return out

    def _needs_verify(self, intents: set[str]) -> bool:
        """Only verify something that was actually changed by this request."""
        if "verify" in intents:
            return True
        if "implement" not in intents:
            return False
        return bool(self.test_command) or "test" in intents

    def _build_tasks(self, text: str, roles: list[str], signals: dict[str, Any]) -> list[TaskSpec]:
        tasks: list[TaskSpec] = []
        previous = ""
        for index, role_name in enumerate(roles, start=1):
            role = self.policy.roles[role_name]
            task_id = f"S{index:02d}"
            budget, minutes = _ROLE_BUDGET.get(role_name, (25000, 20))
            scope: list[str] = []
            if role.write:
                scope = list(signals["paths"]) or ["**"]
            commands: list[str] = []
            if role_name == "verify" and self.test_command:
                commands = [self.test_command]
            tasks.append(TaskSpec(
                task_id=task_id,
                title=f"{_ROLE_TITLES.get(role_name, role_name)} · {self._short(text)}",
                objective=self._objective(role_name, text, signals),
                type=role.task_type,
                role=role_name,
                dependencies=[previous] if previous else [],
                scope=scope,
                out_of_scope=[],
                expected_outputs=["summary", "evidence"],
                acceptance_criteria=list(_ROLE_ACCEPTANCE.get(role_name, [])),
                verification_commands=commands,
                token_budget=int(budget),
                time_limit_minutes=int(minutes),
                retry_limit=1,
                risk_level="medium" if (role.write and not signals["paths"]) else "low",
                approval_policy="auto",
                priority=max(1, 9 - index),
            ))
            previous = task_id
        return tasks

    @staticmethod
    def _objective(role_name: str, text: str, signals: dict[str, Any]) -> str:
        base = _ROLE_OBJECTIVES.get(role_name, "")
        scope_hint = ""
        if signals["paths"]:
            scope_hint = f"优先关注这些文件：{', '.join(signals['paths'][:8])}。"
        return f"用户需求：{text}\n\n本阶段任务：{base}\n{scope_hint}".strip()

    @staticmethod
    def _short(text: str, limit: int = 40) -> str:
        single = re.sub(r"\s+", " ", text).strip()
        return single if len(single) <= limit else single[: limit - 1] + "…"

    @staticmethod
    def _confidence(signals: dict[str, Any], roles: list[str]) -> float:
        if not signals["intents"]:
            return 0.35
        if signals["question"]:
            return 0.7
        if len(signals["intents"]) >= 2 or signals["paths"]:
            return 0.9
        return 0.75


__all__ = ["Segment", "Segmenter"]
