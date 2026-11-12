"""Role policy: which model plays which part, and what it may do.

This is the AgentMesh equivalent of two things the user already uses:

* **OpenCode's ``agent`` block** — a named role (``build`` / ``explore`` /
  ``research`` / ``writer`` / ``plan`` / ``reviewer`` / ``compaction``) with its
  own model, tool permissions and temperature;
* **DSH's ``AGENTS.md``** — a routing *policy*: cheapest sufficient tier first,
  explicit escalation conditions, and a forbidden list (never use a premium
  model for a search, never start two sub-agents with the same job).

AgentMesh turns that from prose into a deterministic structure the controller
can execute and audit:

    Role  -> ordered ladder of (tier, agent, model) candidates

Resolution only ever returns a model name that is **present in the agent's real
catalog** (discovered from CLI/config/session or registered by the user).  The
policy can express intent ("a free model", "a strong model") but it can never
invent a model that the machine does not actually have.

Tier semantics (cost, not capability marketing):

===== ============ ==========================================================
tier  label        typical source
===== ============ ==========================================================
0     local        llama.cpp / local GGUF served on 127.0.0.1
1     free         provider free tier (``*-free`` models)
2     cheap        flash / mini / lite / turbo models
3     standard     general flagship models
4     premium      pro / max / reasoning flagship (points are expensive)
===== ============ ==========================================================
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..connectors import (
    TASK_CAPABILITY_REQUIREMENTS, derive_capability_list, is_auto_executable,
)
from ..errors import ValidationError
from ..models import AgentRecord, TaskType
from ..util import read_json, read_text, write_json
from .model_health import is_degraded

TIER_LABELS: dict[int, str] = {
    0: "local",
    1: "free",
    2: "cheap",
    3: "standard",
    4: "premium",
}
TIER_ORDER = (0, 1, 2, 3, 4)

MODEL_CLASSES = ("local", "free", "cheap", "strong", "standard")

# Local checkpoint / llama.cpp naming, provider free tiers, small fast models,
# and reasoning flagships.  Order matters: the first match wins.
_CLASS_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("local", ("gguf", "llama.cpp", "llm-local", "qwen-local", "ollama", "lmstudio")),
    ("free", ("free",)),
    ("cheap", ("flash", "mini", "lite", "haiku", "turbo", "small", "air", "lightning")),
    ("strong", ("pro", "max", "opus", "ultra", "reason", "o3", "k3.1")),
)


def classify_model(name: str) -> str:
    """Best-effort *class* of a real model name (never used to invent names)."""
    text = str(name or "").strip().lower()
    if not text:
        return "standard"
    for label, keys in _CLASS_RULES:
        if any(key in text for key in keys):
            return label
    return "standard"


# 2026-11-07 defect #6: a free-tier model spent 2x120 minutes on a Blender/GLB
# rebuild that a standard model finished in 51-85 minutes.  Heavy work is
# detected from the task's own words/commands, and free tiers are skipped for
# it at planning/recommendation time (never silently mid-execution).
HEAVY_TASK_MARKERS = (
    "blender", ".glb", ".gltf", "三维", "3d", "重建", "渲染", "实机",
    "帧率", "fps", "draw call", "三角面", "场景搭建", "大规模扫描", "全量扫描",
)


def is_heavy_task(*texts: str) -> bool:
    blob = " ".join(str(t or "") for t in texts).lower()
    return any(marker in blob for marker in HEAVY_TASK_MARKERS)


def spec_is_heavy(spec: Any) -> bool:
    return is_heavy_task(
        getattr(spec, "title", ""), getattr(spec, "objective", ""),
        " ".join(getattr(spec, "verification_commands", None) or []),
        " ".join(getattr(spec, "scope", None) or []))


def _access_rank(level: str) -> int:
    """L0..L4 -> 0..4 for stable ordering; unknown levels rank lowest."""
    text = str(level or "").strip().upper()
    return int(text[1]) if len(text) == 2 and text[0] == "L" and text[1].isdigit() else 0


def _model_variants(model: str) -> list[str]:
    """Names a provider-qualified model can be matched by, most specific first."""
    raw = str(model or "").strip()
    if not raw:
        return []
    out = [raw]
    if "/" in raw:
        out.append(raw.split("/", 1)[1])
        out.append(raw.rsplit("/", 1)[-1])
    base = out[-1]
    if base:
        out.append(Path(base).name)
    seen: set[str] = set()
    ordered: list[str] = []
    for item in out:
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            ordered.append(item)
    return ordered


# ---------------------------------------------------------------------------
# Policy structure
# ---------------------------------------------------------------------------


@dataclass
class TierRule:
    """One rung of a role's ladder."""

    tier: int = 2
    agents: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    model_match: list[str] = field(default_factory=list)
    model_class: list[str] = field(default_factory=list)
    require: list[str] = field(default_factory=list)
    note: str = ""

    def validate(self) -> None:
        if int(self.tier) not in TIER_ORDER:
            raise ValidationError(f"未知成本档位 {self.tier}（允许 0..4）")
        for attr in ("agents", "models", "model_match", "model_class", "require"):
            value = getattr(self, attr)
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                raise ValidationError(f"TierRule.{attr} 必须是字符串列表")
        for item in self.model_class:
            if item not in MODEL_CLASSES:
                raise ValidationError(
                    f"未知模型类别 {item}",
                    details={"allowed": list(MODEL_CLASSES)},
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": int(self.tier), "agents": list(self.agents), "models": list(self.models),
            "model_match": list(self.model_match), "model_class": list(self.model_class),
            "require": list(self.require), "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TierRule":
        if not isinstance(data, dict):
            raise ValidationError("TierRule 必须是对象")
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise ValidationError(f"TierRule 包含未知字段: {sorted(unknown)}")
        rule = cls(
            tier=int(data.get("tier", 2)),
            agents=[str(v) for v in (data.get("agents") or [])],
            models=[str(v) for v in (data.get("models") or [])],
            model_match=[str(v) for v in (data.get("model_match") or [])],
            model_class=[str(v) for v in (data.get("model_class") or [])],
            require=[str(v) for v in (data.get("require") or [])],
            note=str(data.get("note") or ""),
        )
        rule.validate()
        return rule


@dataclass
class Role:
    name: str
    task_type: str = TaskType.ANALYSIS.value
    write: bool = False
    temperature: float = 0.3
    description: str = ""
    prompt: str = ""
    max_tier: int = 4
    ladder: list[TierRule] = field(default_factory=list)

    def validate(self) -> None:
        if not re.match(r"^[a-z][a-z0-9_]{1,31}$", self.name):
            raise ValidationError(f"角色名非法: {self.name}")
        if self.task_type not in {t.value for t in TaskType}:
            raise ValidationError(f"{self.name}: 未知任务类型 {self.task_type}")
        if not self.ladder:
            raise ValidationError(f"{self.name}: 角色至少需要一个档位")
        for rule in self.ladder:
            rule.validate()
        if int(self.max_tier) not in TIER_ORDER:
            raise ValidationError(f"{self.name}: max_tier 必须在 0..4")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type, "write": bool(self.write),
            "temperature": float(self.temperature), "description": self.description,
            "prompt": self.prompt, "max_tier": int(self.max_tier),
            "ladder": [r.to_dict() for r in self.ladder],
        }

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "Role":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise ValidationError(f"角色 {name} 包含未知字段: {sorted(unknown)}")
        role = cls(
            name=name,
            task_type=str(data.get("task_type") or TaskType.ANALYSIS.value),
            write=bool(data.get("write", False)),
            temperature=float(data.get("temperature", 0.3)),
            description=str(data.get("description") or ""),
            prompt=str(data.get("prompt") or ""),
            max_tier=int(data.get("max_tier", 4)),
            ladder=[TierRule.from_dict(item) for item in (data.get("ladder") or [])],
        )
        role.validate()
        return role


@dataclass
class Candidate:
    role: str
    tier: int
    agent_id: str
    agent_name: str
    model: str
    adapter_type: str = ""
    require: list[str] = field(default_factory=list)
    why: str = ""

    @property
    def tier_label(self) -> str:
        return TIER_LABELS.get(self.tier, str(self.tier))

    @property
    def key(self) -> str:
        return f"{self.agent_id}::{self.model}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role, "tier": self.tier, "tier_label": self.tier_label,
            "agent_id": self.agent_id, "agent_name": self.agent_name,
            "model": self.model, "adapter_type": self.adapter_type,
            "require": list(self.require), "why": self.why,
        }


DEFAULT_LADDERS: dict[str, dict[str, Any]] = {
    "explore": {
        "task_type": "analysis", "write": False, "temperature": 0.2,
        "description": "只读探索：搜索代码库、理清结构、定位符号与依赖，把结论交给后续角色",
        "ladder": [
            {"tier": 0, "model_class": ["local"], "require": ["read_files"],
             "note": "本地模型足够，且完全免费"},
            {"tier": 1, "model_class": ["free"], "require": ["read_files"]},
            {"tier": 2, "model_class": ["cheap"], "require": ["read_files"]},
        ],
    },
    "research": {
        "task_type": "analysis", "write": False, "temperature": 0.4,
        "description": "外部调研：官方文档、API 用法、issue 与发布说明，只读不改项目",
        "ladder": [
            {"tier": 1, "model_class": ["free"], "require": ["read_files"]},
            {"tier": 2, "model_class": ["cheap"], "require": ["read_files"]},
            {"tier": 3, "model_class": ["standard"], "require": ["read_files"]},
        ],
    },
    "implement": {
        "task_type": "implementation", "write": True, "temperature": 0.2,
        "description": "执行：按 scope 改代码、补测试、跑命令，产出结构化成果包",
        "ladder": [
            {"tier": 1, "model_class": ["free"],
             "require": ["read_files", "write_files", "terminal"]},
            {"tier": 2, "model_class": ["cheap"],
             "require": ["read_files", "write_files", "terminal"]},
            {"tier": 3, "model_class": ["standard"],
             "require": ["read_files", "write_files", "terminal"]},
            {"tier": 4, "model_class": ["strong"],
             "require": ["read_files", "write_files", "terminal"]},
        ],
    },
    "review": {
        "task_type": "review", "write": False, "temperature": 0.1,
        "description": "审阅：只提问题与建议，按严重程度排序，不改文件",
        "ladder": [
            {"tier": 2, "model_class": ["cheap"], "require": ["read_files", "code_review"]},
            {"tier": 3, "model_class": ["standard"], "require": ["read_files", "code_review"]},
            {"tier": 4, "model_class": ["strong"], "require": ["read_files", "code_review"]},
        ],
    },
    "plan": {
        "task_type": "architecture", "write": False, "temperature": 0.4,
        "description": "规划：架构、模块边界、迁移与回滚、验收标准，只输出方案",
        "ladder": [
            {"tier": 2, "model_class": ["cheap"],
             "require": ["read_files", "architecture", "code_review"]},
            {"tier": 3, "model_class": ["standard"],
             "require": ["read_files", "architecture", "code_review"]},
            {"tier": 4, "model_class": ["strong"],
             "require": ["read_files", "architecture", "code_review"]},
        ],
    },
    "write": {
        "task_type": "documentation", "write": True, "temperature": 0.5,
        "description": "写作：正式文档、报告初稿、README，需要文笔但不需要深度推理",
        "ladder": [
            {"tier": 1, "model_class": ["free"],
             "require": ["read_files", "write_files", "documentation"]},
            {"tier": 2, "model_class": ["cheap"],
             "require": ["read_files", "write_files", "documentation"]},
            {"tier": 3, "model_class": ["standard"],
             "require": ["read_files", "write_files", "documentation"]},
        ],
    },
    "verify": {
        "task_type": "verification", "write": False, "temperature": 0.1,
        "description": "独立验证：复跑命令、检查范围与证据，不采信执行方的自述",
        "ladder": [
            {"tier": 2, "model_class": ["cheap"],
             "require": ["read_files", "terminal", "structured_output"]},
            {"tier": 3, "model_class": ["standard"],
             "require": ["read_files", "terminal", "structured_output"]},
            {"tier": 4, "model_class": ["strong"],
             "require": ["read_files", "terminal", "structured_output"]},
        ],
    },
    "summarize": {
        "task_type": "analysis", "write": False, "temperature": 0.2,
        "description": "汇总：把各任务成果压缩成一份用户可读的最终答案",
        "ladder": [
            {"tier": 3, "model_class": ["standard"], "require": ["read_files"]},
            {"tier": 4, "model_class": ["strong"], "require": ["read_files"]},
        ],
    },
}


@dataclass
class RolePolicy:
    version: int = 1
    name: str = "builtin-balanced"
    roles: dict[str, Role] = field(default_factory=dict)
    limits: dict[str, dict[str, Any]] = field(default_factory=dict)
    premium_tier: int = 3
    max_premium_tasks_per_run: int = 2
    source: str = "builtin"

    # -- construction ---------------------------------------------------
    @classmethod
    def default(cls) -> "RolePolicy":
        roles = {
            name: Role.from_dict(name, dict(spec))
            for name, spec in DEFAULT_LADDERS.items()
        }
        return cls(
            version=1, name="builtin-balanced", roles=roles,
            limits={}, premium_tier=3, max_premium_tasks_per_run=2,
            source="builtin",
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source: str = "file") -> "RolePolicy":
        if not isinstance(data, dict):
            raise ValidationError("角色策略必须是 JSON 对象")
        allowed = {"version", "name", "roles", "limits", "premium_tier",
                   "max_premium_tasks_per_run", "source"}
        unknown = set(data) - allowed
        if unknown:
            raise ValidationError(f"角色策略包含未知字段: {sorted(unknown)}")
        roles_raw = data.get("roles")
        if not isinstance(roles_raw, dict) or not roles_raw:
            raise ValidationError("角色策略 roles 必须是非空对象")
        policy = cls(
            version=int(data.get("version", 1)),
            name=str(data.get("name") or "custom"),
            roles={name: Role.from_dict(name, dict(spec))
                   for name, spec in roles_raw.items()},
            limits=dict(data.get("limits") or {}),
            premium_tier=int(data.get("premium_tier", 3)),
            max_premium_tasks_per_run=int(data.get("max_premium_tasks_per_run", 2)),
            source=source,
        )
        if policy.premium_tier not in TIER_ORDER:
            raise ValidationError("premium_tier 必须在 0..4")
        if policy.max_premium_tasks_per_run < 0:
            raise ValidationError("max_premium_tasks_per_run 不能为负")
        return policy

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "premium_tier": self.premium_tier,
            "max_premium_tasks_per_run": self.max_premium_tasks_per_run,
            "limits": self.limits,
            "roles": {name: role.to_dict() for name, role in self.roles.items()},
        }

    # -- persistence ----------------------------------------------------
    @classmethod
    def load(cls, project_root: str | Path | None = None,
             *, extra_path: str | Path | None = None) -> "RolePolicy":
        """Builtin -> ``~/.agentloop/roles.json`` -> ``<project>/.agentloop/roles.json``.

        ``extra_path`` (or ``AGENTLOOP_ROLES``) wins over both when given.
        """
        policy = cls.default()
        global_path = Path.home() / ".agentloop" / "roles.json"
        if global_path.exists():
            policy = policy.merged(read_json(global_path, {}) or {}, source=str(global_path))
        if project_root:
            project_path = Path(project_root) / ".agentloop" / "roles.json"
            if project_path.exists():
                policy = policy.merged(read_json(project_path, {}) or {},
                                       source=str(project_path))
        if extra_path:
            path = Path(extra_path)
            if not path.exists():
                raise ValidationError(f"角色策略文件不存在: {path}")
            policy = policy.merged(read_json(path, {}) or {}, source=str(path))
        return policy

    def merged(self, overlay: dict[str, Any], *, source: str = "") -> "RolePolicy":
        """Overlay roles one by one; a role present in the overlay replaces the
        builtin role entirely (predictable, no half-merged ladders)."""
        data = self.to_dict()
        if not isinstance(overlay, dict):
            raise ValidationError("角色策略覆盖必须是对象")
        for key in ("name", "premium_tier", "max_premium_tasks_per_run"):
            if key in overlay and overlay[key] is not None:
                data[key] = overlay[key]
        if isinstance(overlay.get("limits"), dict):
            merged = dict(data.get("limits") or {})
            merged.update(overlay["limits"])
            data["limits"] = merged
        roles = overlay.get("roles") or {}
        if not isinstance(roles, dict):
            raise ValidationError("roles 必须是对象")
        merged_roles = dict(data.get("roles") or {})
        for name, spec in roles.items():
            if spec is None:
                continue
            merged_roles[name] = spec
        data["roles"] = merged_roles
        policy = type(self).from_dict(data, source=source or self.source)
        return policy

    def save(self, path: str | Path) -> dict[str, Any]:
        write_json(path, self.to_dict(), indent=2)
        return {"path": str(path), "name": self.name, "roles": sorted(self.roles)}

    # -- queries --------------------------------------------------------
    def role(self, name: str) -> Role:
        role = self.roles.get(str(name))
        if role is None:
            raise ValidationError(
                f"未知角色 {name}", details={"allowed": sorted(self.roles)}
            )
        return role

    def role_names(self) -> list[str]:
        return list(self.roles)

    def limits_for(self, agent_id: str) -> dict[str, Any]:
        return dict(self.limits.get(str(agent_id)) or {})

    def is_premium(self, tier: int) -> bool:
        return int(tier) >= int(self.premium_tier)

    def candidates(self, role_name: str, *, agents: list[AgentRecord],
                   catalog: dict[str, list[str]], exclude: Iterable[str] = (),
                   min_tier: int = 0, max_tier: int | None = None,
                   require_auto: bool = True, limit: int = 0,
                   heavy: bool = False) -> list[Candidate]:
        """Expand a role's ladder against the *real* catalog.

        A ladder rung with no matching model simply produces nothing; the next
        rung is tried. That is what makes the policy survive a model being
        removed or an agent going offline.
        """
        role = self.role(role_name)
        excluded = {str(a) for a in exclude if str(a)}
        base_required = set(TASK_CAPABILITY_REQUIREMENTS.get(role.task_type, ["read_files"]))
        if role.write:
            base_required.add("write_files")
        ceiling = int(role.max_tier if max_tier is None else max_tier)
        out: list[Candidate] = []
        seen: set[str] = set()
        for rule in role.ladder:
            if int(rule.tier) < int(min_tier) or int(rule.tier) > ceiling:
                continue
            required = sorted(base_required | set(rule.require))
            for agent_rank, agent in enumerate(self._agents_in_rule(rule, agents)):
                if agent.agent_id in excluded:
                    continue
                if agent.status in ("offline", "error"):
                    continue
                caps = set(derive_capability_list(agent))
                if not set(required) <= caps:
                    continue
                if require_auto and not is_auto_executable(agent):
                    continue
                for model_rank, model in enumerate(self._models_in_rule(rule, catalog, agent)):
                    if is_degraded(agent, model):
                        continue
                    if heavy and classify_model(model) == "free":
                        continue
                    key = f"{agent.agent_id}::{model}"
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(Candidate(
                        role=role.name, tier=int(rule.tier),
                        agent_id=agent.agent_id, agent_name=agent.name, model=model,
                        adapter_type=agent.adapter_type, require=required,
                        why=self._why(rule, agent.agent_id, model),
                    ))
        # The ladder *is* the order: rungs in policy order, then the order the
        # rung lists agents and models. Two runs over the same catalog always
        # produce the same list, and escalation can simply walk it.
        if limit and limit > 0:
            return out[: int(limit)]
        return out

    def _agents_in_rule(self, rule: TierRule, agents: list[AgentRecord]) -> list[AgentRecord]:
        if not rule.agents:
            ordered = sorted(agents, key=lambda a: (-_access_rank(a.access_level), a.agent_id))
            return ordered
        ordered: list[AgentRecord] = []
        for pattern in rule.agents:
            for agent in agents:
                if agent in ordered:
                    continue
                if fnmatch.fnmatch(agent.agent_id.lower(), pattern.lower()):
                    ordered.append(agent)
        return ordered

    def _models_in_rule(self, rule: TierRule, catalog: dict[str, list[str]],
                        agent: AgentRecord) -> list[str]:
        available = list(catalog.get(agent.agent_id) or agent.models or [])
        if not available:
            return []
        picked: list[str] = []
        # 1) explicit model names (exact, case-insensitive)
        for wanted in rule.models:
            for model in available:
                if model.lower() == wanted.lower() and model not in picked:
                    picked.append(model)
        # 2) glob patterns
        for pattern in rule.model_match:
            for model in available:
                if model in picked:
                    continue
                if fnmatch.fnmatch(model.lower(), pattern.lower()):
                    picked.append(model)
        # 3) model classes
        for label in rule.model_class:
            for model in available:
                if model in picked:
                    continue
                if classify_model(model) == label:
                    picked.append(model)
        # A rung with no selector at all means "any real model of this agent".
        if not (rule.models or rule.model_match or rule.model_class):
            picked = list(available)
        return picked

    @staticmethod
    def _why(rule: TierRule, agent_id: str, model: str) -> str:
        bits = [f"tier {rule.tier}({TIER_LABELS.get(rule.tier, rule.tier)})"]
        if rule.models:
            bits.append(f"指定模型 {rule.models}")
        if rule.model_match:
            bits.append(f"匹配 {rule.model_match}")
        if rule.model_class:
            bits.append(f"类别 {rule.model_class}")
        bits.append(f"实际命中 {agent_id}/{model}（{classify_model(model)}）")
        if rule.note:
            bits.append(rule.note)
        return "；".join(bits)

    def escalate(self, role_name: str, current: Candidate | None, *, agents: list[AgentRecord],
                 catalog: dict[str, list[str]], tried: Iterable[str] = (),
                 require_auto: bool = True) -> Candidate | None:
        """Next candidate *after* the current one on the role's ladder.

        Escalation is intentional and bounded: a candidate is never retried
        twice for the same task, and the walk always moves forward (cheaper
        rungs are not revisited), which is what keeps a flaky free model from
        eating the whole budget.
        """
        tried_keys = {str(k) for k in tried} | ({current.key} if current else set())
        ladder = self.candidates(role_name, agents=agents, catalog=catalog,
                                 require_auto=require_auto)
        start = 0
        if current is not None:
            for index, candidate in enumerate(ladder):
                if candidate.key == current.key:
                    start = index + 1
                    break
        forward = [c for c in ladder[start:]
                   if c.key not in tried_keys
                   and (current is None or c.tier >= current.tier)]
        if forward:
            return forward[0]
        # Nothing at or above the current tier: degrade to any untried rung
        # rather than failing the task, and let the caller record the downgrade.
        for candidate in ladder[start:]:
            if candidate.key not in tried_keys:
                return candidate
        return None

    def with_overrides(self, *, executor: tuple[str, str] = ("", ""),
                       commander: tuple[str, str] = ("", ""),
                       catalog: dict[str, list[str]] | None = None) -> "RolePolicy":
        """Pin the two roles the user actually cares about for one session.

        ``executor`` fronts every non-decision role (explore / research /
        implement / write / verify); ``commander`` fronts the decision roles
        (plan / review / summarize). Pinning is a *ladder entry at the front*,
        not a hard replacement, so the rest of the ladder remains as fallback
        when that agent is offline or its execution fails.

        When ``catalog`` is given the pin is validated against the real model
        list — a pin to a model the agent does not have is rejected instead of
        being silently ignored at run time.
        """
        executor_agent, executor_model = (str(executor[0] or ""), str(executor[1] or ""))
        commander_agent, commander_model = (str(commander[0] or ""), str(commander[1] or ""))
        for agent_id, model, label in (
            (executor_agent, executor_model, "执行模型"),
            (commander_agent, commander_model, "统帅模型"),
        ):
            if not agent_id:
                continue
            if not model:
                raise ValidationError(f"{label}指定了 Agent {agent_id} 但没有指定模型")
            if catalog is not None:
                known = [m.lower() for m in (catalog.get(agent_id) or [])]
                if model.lower() not in known:
                    raise ValidationError(
                        f"{agent_id} 的真实模型清单中没有 {model}（{label}）",
                        details={"available": sorted(catalog.get(agent_id) or [])[:40]},
                    )
        data = self.to_dict()
        plan_roles = data.get("roles") or {}
        if executor_agent:
            rung = {"tier": _tier_for_class(executor_model), "agents": [executor_agent],
                    "models": [executor_model], "model_match": [], "model_class": [],
                    "require": [], "note": "会话指定的执行模型（优先使用其免费额度）"}
            for name in ("explore", "research", "implement", "write", "verify"):
                if name in plan_roles:
                    plan_roles[name]["ladder"] = [rung] + list(plan_roles[name]["ladder"])
        if commander_agent:
            rung = {"tier": _tier_for_class(commander_model), "agents": [commander_agent],
                    "models": [commander_model], "model_match": [], "model_class": [],
                    "require": [], "note": "会话指定的统帅模型（只做决策与汇总）"}
            for name in ("plan", "review", "summarize"):
                if name in plan_roles:
                    plan_roles[name]["ladder"] = [rung] + list(plan_roles[name]["ladder"])
        data["roles"] = plan_roles
        policy = type(self).from_dict(data, source=f"{self.source}+overrides")
        return policy

    def explain(self, *, agents: list[AgentRecord],
                catalog: dict[str, list[str]]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for name, role in self.roles.items():
            candidates = self.candidates(name, agents=agents, catalog=catalog, limit=6)
            rows.append({
                "role": name, "task_type": role.task_type, "write": role.write,
                "description": role.description,
                "resolved": [c.to_dict() for c in candidates],
                "resolved_count": len(candidates),
                "status": "ready" if candidates else "no_candidate",
            })
        return {
            "name": self.name, "source": self.source,
            "premium_tier": self.premium_tier,
            "max_premium_tasks_per_run": self.max_premium_tasks_per_run,
            "limits": self.limits,
            "tiers": TIER_LABELS,
            "roles": rows,
        }


# ---------------------------------------------------------------------------
# Importers: turn the user's existing OpenCode / DSH configuration into a policy
# ---------------------------------------------------------------------------

_OPENCODE_ROLE_MAP: dict[str, str] = {
    "build": "implement",
    "general": "implement",
    "explore": "explore",
    "research": "research",
    "writer": "write",
    "plan": "plan",
    "reviewer": "review",
    "compaction": "summarize",
    "title": "summarize",
    "summary": "summarize",
}

_AGENTS_MD_ROLE_MAP: dict[str, str] = {
    "build": "implement",
    "general": "implement",
    "explore": "explore",
    "research": "research",
    "writer": "write",
    "plan": "plan",
    "reviewer": "review",
    "review": "review",
    "summarize": "summarize",
}


def _tier_for_class(model: str) -> int:
    return {"local": 0, "free": 1, "cheap": 2, "standard": 3, "strong": 4}[classify_model(model)]


def _locate_model(hint: str, catalog: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Find real (agent_id, model) pairs a configured model string refers to.

    Exact/segment matches win over fuzzy ones: if any agent has a model whose
    name *is* the hinted name (or its last path segment), the fuzzy substring
    results are discarded rather than adding look-alike variants.
    """
    variants = [v.lower() for v in _model_variants(hint)]
    exact: list[tuple[str, str]] = []
    fuzzy: list[tuple[str, str]] = []
    for agent_id, models in catalog.items():
        for model in models:
            low = model.lower()
            if low in variants or low.rsplit("/", 1)[-1] in variants:
                exact.append((agent_id, model))
            elif any(v and (low.endswith("/" + v) or v in low) and len(v) >= 8
                     for v in variants):
                fuzzy.append((agent_id, model))
    return exact or fuzzy


def _overlay_from_role_models(role_models: dict[str, list[str]], *,
                              catalog: dict[str, list[str]],
                              source: str) -> dict[str, Any]:
    """Build a policy overlay: each role prefers the imported (agent, model)."""
    overlay_roles: dict[str, Any] = {}
    matched: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for role_name, hints in role_models.items():
        if role_name not in DEFAULT_LADDERS:
            continue
        spec = json.loads(json.dumps(DEFAULT_LADDERS[role_name]))
        preferred: list[dict[str, Any]] = []
        seen: set[str] = set()
        for hint in hints:
            hits = _locate_model(hint, catalog)
            if not hits:
                unmatched.append(hint)
                continue
            for agent_id, model in hits:
                key = f"{agent_id}::{model}"
                if key in seen:
                    continue
                seen.add(key)
                preferred.append({
                    "tier": _tier_for_class(model), "agents": [agent_id],
                    "models": [model],
                    "require": [],
                    "note": f"从 {source} 导入：{hint}",
                })
                matched.append({"role": role_name, "hint": hint,
                                "agent_id": agent_id, "model": model,
                                "tier": _tier_for_class(model)})
        if preferred:
            # Imported preference first, then the original builtin ladder as
            # fallback so the role still resolves if that agent goes offline.
            # Duplicates are removed by ``candidates()``.
            spec["ladder"] = preferred + list(spec.get("ladder") or [])
            overlay_roles[role_name] = spec
    return {"roles": overlay_roles, "matched": matched, "unmatched": unmatched}


def policy_from_opencode(path: str | Path, *, catalog: dict[str, list[str]]) -> dict[str, Any]:
    """Read an ``opencode.json`` and map its agents to role preferences."""
    data = read_json(path, {}) or {}
    if not isinstance(data, dict) or not data:
        raise ValidationError(f"无法读取 opencode 配置: {path}")
    agents = data.get("agent") or {}
    if not isinstance(agents, dict):
        raise ValidationError("opencode 配置缺少 agent 对象")
    role_models: dict[str, list[str]] = {}
    for name, spec in agents.items():
        role = _OPENCODE_ROLE_MAP.get(str(name).lower())
        if not role or not isinstance(spec, dict):
            continue
        model = str(spec.get("model") or "")
        if model:
            role_models.setdefault(role, []).append(model)
    result = _overlay_from_role_models(role_models, catalog=catalog, source=str(path))
    result["source"] = str(path)
    result["roles_found"] = sorted(role_models)
    return result


_MODEL_HINT = re.compile(r"`([^`]+/[^`]+)`")


def policy_from_agents_md(path: str | Path, *, catalog: dict[str, list[str]]) -> dict[str, Any]:
    """Read a DSH-style ``AGENTS.md`` routing policy.

    The document is prose, so the parser is deliberately narrow: it keeps the
    ``### Role`` headings, collects backticked ``provider/model`` tokens inside
    each section, and never guesses a model from free text.
    """
    text = read_text(path, default="")
    if not text.strip():
        raise ValidationError(f"无法读取 AGENTS.md: {path}")
    role_models: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        heading = re.match(r"^#{2,4}\s+([A-Za-z][\w -]*)\s*$", line.strip())
        if heading:
            current = _AGENTS_MD_ROLE_MAP.get(heading.group(1).strip().lower(), "")
            continue
        if not current:
            continue
        for hint in _MODEL_HINT.findall(line):
            if hint not in role_models.setdefault(current, []):
                role_models[current].append(hint)
    result = _overlay_from_role_models(role_models, catalog=catalog, source=str(path))
    result["source"] = str(path)
    result["roles_found"] = sorted(role_models)
    return result


__all__ = [
    "TIER_LABELS", "MODEL_CLASSES", "classify_model",
    "TierRule", "Role", "Candidate", "RolePolicy",
    "policy_from_opencode", "policy_from_agents_md",
]
