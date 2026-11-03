"""Core data objects and protocol validation.

The field names mirror the MVP PRD exactly (FR-TP-003, FR-AR-001, FR-VR-002,
FR-CU etc.). Validation lives here so every adapter and the control plane share
one contract.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any, Iterable

from .errors import ValidationError
from .util import new_id, now_iso, sha1_hex, stable_json


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class AccessLevel(str, Enum):
    L0 = "L0"  # manual relay only
    L1 = "L1"  # shared files
    L2 = "L2"  # structured task / structured result
    L3 = "L3"  # session-level collaboration
    L4 = "L4"  # native bidirectional control

    @property
    def rank(self) -> int:
        return int(self.value[1])


class AdapterType(str, Enum):
    CLI = "cli"
    OPENCODE = "opencode"
    FILE_RELAY = "file_relay"
    MANUAL = "manual"
    SESSION_ONLY = "session_only"
    MCP = "mcp"
    GUI_AUTOMATION = "gui_automation"


class AgentStatus(str, Enum):
    ONLINE = "online"
    BUSY = "busy"
    OFFLINE = "offline"
    ERROR = "error"
    UNKNOWN = "unknown"


class TaskType(str, Enum):
    ANALYSIS = "analysis"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"
    DOCUMENTATION = "documentation"
    REVIEW = "review"
    ARCHITECTURE = "architecture"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TaskStatus(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_AGENT = "waiting_agent"
    WAITING_USER = "waiting_user"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELED = "canceled"
    PAUSED = "paused"
    BLOCKED = "blocked"


class RunStatus(str, Enum):
    PLANNING = "planning"
    PENDING_APPROVAL = "pending_approval"
    READY = "ready"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    PENDING_ACCEPTANCE = "pending_acceptance"
    PENDING_MERGE = "pending_merge"
    MERGING = "merging"
    COMPLETED = "completed"
    RELEASED = "released"
    FAILED = "failed"
    CANCELED = "canceled"
    PAUSED = "paused"


class PlanStatus(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


class ApprovalKind(str, Enum):
    PLAN = "plan"
    ARCHITECTURE = "architecture"
    MERGE = "merge"
    ACCEPTANCE = "acceptance"
    RELEASE = "release"
    HIGH_RISK = "high_risk"
    BUDGET = "budget"
    MAIN_SWITCH = "main_switch"
    PERMISSION = "permission"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class VerificationStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"
    SKIPPED = "skipped"


class ChangeStatus(str, Enum):
    PROPOSED = "proposed"
    APPLIED = "applied"
    REVERTED = "reverted"
    CONFLICT = "conflict"


class MemoryType(str, Enum):
    """Unified memory categories aligned with 需求整理-v1 Level 1-5.

    Canonical categories cover chat messages, task state, project index, code
    structure, knowledge and decision process. Workflow state and user
    preferences are first-class because they are injected at agent startup.
    Legacy names are enum aliases so older workspaces keep working.
    """

    MESSAGE = "message"                  # Level 1 聊天记录
    TASK_STATE = "task_state"            # Level 2 任务状态
    PROJECT_INDEX = "project_index"      # Level 3 项目索引
    CODE_STRUCTURE = "code_structure"    # Level 3 代码结构
    KNOWLEDGE = "knowledge"              # Level 4 知识库
    DECISION = "decision"                # 决策过程
    WORKFLOW = "workflow"                # Level 5 工作流状态
    USER_PREFERENCE = "user_preference"

    # Legacy aliases (same values as canonical members above).
    PROJECT_FACT = "project_index"
    PROBLEM_SOLUTION = "knowledge"
    TASK_PROCESS = "task_state"
    WORKING = "workflow"


TEST_COMMAND_PATTERNS: tuple[str, ...] = (
    "pytest", "unittest", "npm test", "npm run test", "pnpm test", "yarn test",
    "go test", "cargo test", "mvn test", "gradle test", "dotnet test",
    "phpunit", "rspec", "vitest", "jest",
)


def has_test_command(commands: Iterable[str]) -> bool:
    text = " ".join(str(c).lower() for c in commands)
    return any(pattern in text for pattern in TEST_COMMAND_PATTERNS)


LEGACY_MEMORY_TYPE_ALIASES: dict[str, str] = {
    "project_fact": MemoryType.PROJECT_INDEX.value,
    "problem_solution": MemoryType.KNOWLEDGE.value,
    "task_process": MemoryType.TASK_STATE.value,
    "working": MemoryType.WORKFLOW.value,
}

MEMORY_SYNC_LEVEL: dict[str, int] = {
    MemoryType.MESSAGE.value: 1,
    MemoryType.TASK_STATE.value: 2,
    MemoryType.PROJECT_INDEX.value: 3,
    MemoryType.CODE_STRUCTURE.value: 3,
    MemoryType.KNOWLEDGE.value: 4,
    MemoryType.DECISION.value: 4,
    MemoryType.WORKFLOW.value: 5,
    MemoryType.USER_PREFERENCE.value: 3,
}


def normalize_memory_type(value: str) -> str:
    text = str(value or "").strip()
    return LEGACY_MEMORY_TYPE_ALIASES.get(text, text)


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    CONFLICT = "conflict"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


ALL_TASK_STATUSES = {s.value for s in TaskStatus}
ALL_MEMORY_TYPES = {m.value for m in MemoryType}

# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

TASK_TRANSITIONS: dict[str, set[str]] = {
    TaskStatus.DRAFT.value: {TaskStatus.PENDING_APPROVAL.value, TaskStatus.QUEUED.value, TaskStatus.CANCELED.value},
    TaskStatus.PENDING_APPROVAL.value: {TaskStatus.QUEUED.value, TaskStatus.REJECTED.value, TaskStatus.CANCELED.value},
    TaskStatus.QUEUED.value: {TaskStatus.RUNNING.value, TaskStatus.PAUSED.value, TaskStatus.CANCELED.value, TaskStatus.BLOCKED.value},
    TaskStatus.RUNNING.value: {
        TaskStatus.WAITING_AGENT.value, TaskStatus.WAITING_USER.value, TaskStatus.VERIFYING.value,
        TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.PAUSED.value,
        TaskStatus.BLOCKED.value, TaskStatus.CANCELED.value,
    },
    TaskStatus.WAITING_AGENT.value: {TaskStatus.RUNNING.value, TaskStatus.FAILED.value, TaskStatus.PAUSED.value, TaskStatus.CANCELED.value},
    TaskStatus.WAITING_USER.value: {TaskStatus.RUNNING.value, TaskStatus.VERIFYING.value, TaskStatus.FAILED.value, TaskStatus.PAUSED.value, TaskStatus.CANCELED.value},
    TaskStatus.VERIFYING.value: {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.BLOCKED.value},
    TaskStatus.PAUSED.value: {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value, TaskStatus.CANCELED.value},
    TaskStatus.BLOCKED.value: {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value, TaskStatus.FAILED.value, TaskStatus.CANCELED.value},
    TaskStatus.FAILED.value: {TaskStatus.QUEUED.value, TaskStatus.CANCELED.value, TaskStatus.REJECTED.value},
    TaskStatus.COMPLETED.value: set(),
    TaskStatus.REJECTED.value: set(),
    TaskStatus.CANCELED.value: set(),
}


def assert_task_transition(current: str, target: str) -> None:
    from .errors import StateTransitionError

    if target not in TASK_TRANSITIONS.get(current, set()):
        raise StateTransitionError(f"任务状态不允许从 {current} 变为 {target}", details={"from": current, "to": target})


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _require_type(value: Any, types: tuple[type, ...], name: str) -> None:
    if not isinstance(value, types) or (isinstance(value, bool) and bool not in types):
        raise ValidationError(f"{name} 类型不正确，期望 {types}", details={"field": name, "value": repr(value)[:200]})


def _string_list(value: Any, name: str, *, require_non_empty: bool = False) -> list[str]:
    if value is None:
        value = []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ValidationError(f"{name} 必须是字符串列表", details={"field": name})
    out = [v.strip() for v in value if v.strip()]
    if require_non_empty and not out:
        raise ValidationError(f"{name} 不能为空", details={"field": name})
    return out


def normalize_scope_path(path: str) -> str:
    p = path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    p = re.sub(r"/+", "/", p)
    return p


def scope_matches(pattern: str, path: str) -> bool:
    """Match a PRD-style scope pattern against a repo-relative POSIX path.

    Supports `**` (any depth), `*` (single segment), trailing `/` (directory),
    and plain prefixes. A pattern with no glob character is treated as an
    exact path or a directory prefix.
    """
    pat = normalize_scope_path(pattern)
    rel = normalize_scope_path(path)
    if not pat:
        return False
    if pat.endswith("/"):
        pat = pat.rstrip("/")
    # plain path / directory prefix
    if not any(ch in pat for ch in "*?["):
        return rel == pat or rel.startswith(pat + "/")
    if pat.endswith("/**"):
        prefix = pat[:-3].rstrip("/")
        return rel == prefix or rel.startswith(prefix + "/")
    regex = _glob_to_regex(pat)
    return bool(re.match(regex, rel))


def _glob_to_regex(pattern: str) -> str:
    out = ["^"]
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                out.append(".*")
                i += 2
                if i < len(pattern) and pattern[i] == "/":
                    i += 1
                    out.append("/?")
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(r"\[")
            else:
                out.append(pattern[i:j + 1])
                i = j
        else:
            out.append(re.escape(ch))
        i += 1
    out.append("$")
    return "".join(out)


# ---------------------------------------------------------------------------
# Project / Agent
# ---------------------------------------------------------------------------

@dataclass
class Project:
    project_id: str
    name: str
    root: str
    workspace_id: str
    repository_id: str
    worktree_id: str
    git_remote: str = ""
    branch: str = ""
    commit: str = ""
    tech_stack: list[str] = field(default_factory=list)
    build_command: str = ""
    test_command: str = ""
    status: str = "active"
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id, "name": self.name, "root": self.root,
            "workspace_id": self.workspace_id, "repository_id": self.repository_id,
            "worktree_id": self.worktree_id, "git_remote": self.git_remote,
            "branch": self.branch, "commit": self.commit, "tech_stack": self.tech_stack,
            "build_command": self.build_command, "test_command": self.test_command,
            "status": self.status, "created_at": self.created_at,
            "updated_at": self.updated_at, "extra": self.extra,
        }


@dataclass
class AgentRecord:
    agent_id: str
    name: str
    product: str
    edition: str = ""
    project_id: str = ""
    version: str = "unknown"
    adapter_type: str = AdapterType.FILE_RELAY.value
    adapter_config: dict[str, Any] = field(default_factory=dict)
    access_level: str = AccessLevel.L1.value
    capabilities: dict[str, bool] = field(default_factory=dict)
    models: list[str] = field(default_factory=list)
    permissions: dict[str, Any] = field(default_factory=dict)
    status: str = AgentStatus.UNKNOWN.value
    last_seen: str = ""
    last_error: str = ""
    discovered: bool = False
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    extra: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", self.agent_id):
            raise ValidationError("agent_id 只能包含字母、数字、下划线、点和连字符", details={"agent_id": self.agent_id})
        if self.adapter_type not in {a.value for a in AdapterType}:
            raise ValidationError(f"未知 adapter_type: {self.adapter_type}")
        if self.access_level not in {a.value for a in AccessLevel}:
            raise ValidationError(f"未知 access_level: {self.access_level}")
        if not self.name.strip():
            raise ValidationError("Agent name 不能为空")
        if not isinstance(self.adapter_config, dict):
            raise ValidationError("adapter_config 必须是对象")

    @property
    def is_writable(self) -> bool:
        return bool(self.capabilities.get("write_files")) and "write_files" in self.permissions.get("allow", ["write_files"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id, "name": self.name, "product": self.product,
            "edition": self.edition, "project_id": self.project_id, "version": self.version,
            "adapter_type": self.adapter_type, "adapter_config": self.adapter_config,
            "access_level": self.access_level, "capabilities": self.capabilities,
            "models": self.models, "permissions": self.permissions,
            "status": self.status, "last_seen": self.last_seen,
            "last_error": self.last_error, "discovered": self.discovered,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "extra": self.extra,
        }


# ---------------------------------------------------------------------------
# Task protocol
# ---------------------------------------------------------------------------

@dataclass
class TaskSpec:
    task_id: str
    title: str
    objective: str
    type: str = TaskType.ANALYSIS.value
    suggested_agent: str = ""
    parent_task_id: str = ""
    scope: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    input_artifacts: list[str] = field(default_factory=list)
    expected_outputs: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    verification_commands: list[str] = field(default_factory=list)
    token_budget: int = 30000
    time_limit_minutes: int = 30
    retry_limit: int = 2
    risk_level: str = RiskLevel.LOW.value
    approval_policy: str = "auto"
    priority: int = 5  # 0..10, higher first within the same dependency layer

    @property
    def writable(self) -> bool:
        return self.type in (TaskType.IMPLEMENTATION.value, TaskType.DOCUMENTATION.value)

    @property
    def read_only(self) -> bool:
        return not self.writable

    def validate(self) -> None:
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$", self.task_id):
            raise ValidationError(f"task_id 非法: {self.task_id}")
        if not self.title.strip():
            raise ValidationError(f"{self.task_id}: title 不能为空")
        if not self.objective.strip():
            raise ValidationError(f"{self.task_id}: objective 不能为空")
        if self.type not in {t.value for t in TaskType}:
            raise ValidationError(f"{self.task_id}: 未知任务类型 {self.type}")
        if self.risk_level not in {r.value for r in RiskLevel}:
            raise ValidationError(f"{self.task_id}: 未知风险等级 {self.risk_level}")
        if self.approval_policy not in ("auto", "required", "plan_only"):
            raise ValidationError(f"{self.task_id}: approval_policy 必须是 auto/required/plan_only")
        _require_type(self.priority, int, "priority")
        if not (0 <= int(self.priority) <= 10):
            raise ValidationError(f"{self.task_id}: priority 必须在 0..10")
        _require_type(self.token_budget, (int, float), "token_budget")
        _require_type(self.time_limit_minutes, (int, float), "time_limit_minutes")
        _require_type(self.retry_limit, int, "retry_limit")
        if self.token_budget < 0:
            raise ValidationError(f"{self.task_id}: token_budget 不能为负")
        if self.time_limit_minutes <= 0:
            raise ValidationError(f"{self.task_id}: time_limit_minutes 必须大于 0")
        if self.retry_limit < 0:
            raise ValidationError(f"{self.task_id}: retry_limit 不能为负")
        self.scope = _string_list(self.scope, f"{self.task_id}.scope")
        self.out_of_scope = _string_list(self.out_of_scope, f"{self.task_id}.out_of_scope")
        self.dependencies = _string_list(self.dependencies, f"{self.task_id}.dependencies")
        self.expected_outputs = _string_list(self.expected_outputs, f"{self.task_id}.expected_outputs")
        self.acceptance_criteria = _string_list(self.acceptance_criteria, f"{self.task_id}.acceptance_criteria")
        self.verification_commands = _string_list(self.verification_commands, f"{self.task_id}.verification_commands")
        if self.writable and not self.scope:
            raise ValidationError(f"{self.task_id}: 可写任务必须声明 scope（允许修改的文件范围）")
        if self.writable and self.task_id in self.dependencies:
            raise ValidationError(f"{self.task_id}: 不能依赖自身")
        if self.type == TaskType.ANALYSIS.value and has_test_command(self.verification_commands):
            raise ValidationError(
                f"{self.task_id}: analysis 任务不能携带测试类 verification_commands"
                "（测试命令应放在 implementation/verification 任务）",
                details={"verification_commands": self.verification_commands},
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "title": self.title, "objective": self.objective,
            "type": self.type, "suggested_agent": self.suggested_agent,
            "parent_task_id": self.parent_task_id, "scope": self.scope,
            "out_of_scope": self.out_of_scope, "dependencies": self.dependencies,
            "required_capabilities": self.required_capabilities,
            "input_artifacts": self.input_artifacts, "expected_outputs": self.expected_outputs,
            "acceptance_criteria": self.acceptance_criteria,
            "verification_commands": self.verification_commands,
            "token_budget": int(self.token_budget), "time_limit_minutes": int(self.time_limit_minutes),
            "retry_limit": int(self.retry_limit), "risk_level": self.risk_level,
            "approval_policy": self.approval_policy, "priority": int(self.priority),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskSpec":
        if not isinstance(data, dict):
            raise ValidationError("任务条目必须是对象")
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValidationError(f"任务包含未知字段: {sorted(unknown)}")
        kwargs = {}
        for f in fields(cls):
            if f.name in data:
                kwargs[f.name] = data[f.name]
        spec = cls(**kwargs)
        spec.validate()
        return spec


@dataclass
class TaskPlan:
    plan_id: str
    project_id: str
    goal: str
    tasks: list[TaskSpec]
    constraints: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    nonfunctional: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    main_agent_id: str = ""
    model: str = ""
    status: str = PlanStatus.DRAFT.value
    created_at: str = field(default_factory=now_iso)
    raw: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.goal.strip():
            raise ValidationError("计划 goal 不能为空")
        if not self.tasks:
            raise ValidationError("计划至少需要一个任务")
        ids = [t.task_id for t in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValidationError("计划中存在重复 task_id")
        idset = set(ids)
        for t in self.tasks:
            t.validate()
            for dep in t.dependencies:
                if dep not in idset:
                    raise ValidationError(f"{t.task_id}: 依赖不存在 {dep}")
        self.constraints = _string_list(self.constraints, "constraints")
        self.requirements = _string_list(self.requirements, "requirements")
        self.nonfunctional = _string_list(self.nonfunctional, "nonfunctional")
        self.risks = _string_list(self.risks, "risks")
        self.open_questions = _string_list(self.open_questions, "open_questions")
        self.topological_order()  # raises on cycles

    def topological_order(self) -> list[TaskSpec]:
        by_id = {t.task_id: t for t in self.tasks}
        indegree = {tid: len(t.dependencies) for tid, t in by_id.items()}
        children: dict[str, list[str]] = {tid: [] for tid in by_id}
        for tid, task in by_id.items():
            for dep in task.dependencies:
                if dep not in by_id:
                    raise ValidationError(f"{tid}: 依赖不存在 {dep}")
                children[dep].append(tid)
        queue = sorted([tid for tid, d in indegree.items() if d == 0])
        order: list[TaskSpec] = []
        while queue:
            tid = queue.pop(0)
            order.append(by_id[tid])
            for child in children[tid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
            queue.sort()
        if len(order) != len(by_id):
            cyclic = sorted(tid for tid, d in indegree.items() if d > 0)
            raise ValidationError(f"任务依赖存在环: {cyclic}")
        return order

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_version": 1,
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "goal": self.goal,
            "constraints": self.constraints,
            "requirements": self.requirements,
            "nonfunctional": self.nonfunctional,
            "risks": self.risks,
            "open_questions": self.open_questions,
            "main_agent_id": self.main_agent_id,
            "model": self.model,
            "status": self.status,
            "created_at": self.created_at,
            "tasks": [t.to_dict() for t in self.tasks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, project_id: str = "", plan_id: str = "") -> "TaskPlan":
        if not isinstance(data, dict):
            raise ValidationError("计划必须是 JSON/YAML 对象")
        allowed = {"plan_version", "plan_id", "project_id", "goal", "constraints", "tasks",
                   "requirements", "nonfunctional", "risks", "open_questions",
                   "main_agent_id", "model", "status", "created_at"}
        unknown = set(data) - allowed
        if unknown:
            raise ValidationError(f"计划包含未知字段: {sorted(unknown)}")
        tasks_raw = data.get("tasks")
        if not isinstance(tasks_raw, list):
            raise ValidationError("tasks 必须是数组")
        tasks = [TaskSpec.from_dict(item) for item in tasks_raw]
        plan = cls(
            plan_id=plan_id or str(data.get("plan_id") or new_id("plan")),
            project_id=project_id or str(data.get("project_id") or ""),
            goal=str(data.get("goal") or ""),
            tasks=tasks,
            constraints=list(data.get("constraints") or []),
            requirements=list(data.get("requirements") or []),
            nonfunctional=list(data.get("nonfunctional") or []),
            risks=list(data.get("risks") or []),
            open_questions=list(data.get("open_questions") or []),
            main_agent_id=str(data.get("main_agent_id") or ""),
            model=str(data.get("model") or ""),
            status=str(data.get("status") or PlanStatus.DRAFT.value),
            created_at=str(data.get("created_at") or now_iso()),
            raw=dict(data),
        )
        plan.validate()
        return plan


# ---------------------------------------------------------------------------
# Runtime task
# ---------------------------------------------------------------------------

@dataclass
class Task:
    uid: str
    project_id: str
    plan_id: str
    run_id: str
    spec: TaskSpec
    status: str = TaskStatus.DRAFT.value
    assigned_agent: str = ""
    attempts: int = 0
    workdir: str = ""
    task_dir: str = ""
    base_commit: str = ""
    patch_path: str = ""
    result_path: str = ""
    verification_status: str = VerificationStatus.PENDING.value
    error: str = ""
    pid: int = 0
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    started_at: str = ""
    finished_at: str = ""

    @property
    def task_id(self) -> str:
        return self.spec.task_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid, "project_id": self.project_id, "plan_id": self.plan_id,
            "run_id": self.run_id, "task_id": self.spec.task_id,
            "parent_task_id": self.spec.parent_task_id,
            "title": self.spec.title, "objective": self.spec.objective,
            "type": self.spec.type, "assigned_agent": self.assigned_agent,
            "status": self.status, "attempts": self.attempts,
            "workdir": self.workdir, "task_dir": self.task_dir,
            "base_commit": self.base_commit, "patch_path": self.patch_path,
            "result_path": self.result_path, "verification_status": self.verification_status,
            "error": self.error, "pid": self.pid,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "spec": self.spec.to_dict(),
        }


# ---------------------------------------------------------------------------
# Result / verification packages
# ---------------------------------------------------------------------------

RESULT_REQUIRED_FIELDS: tuple[str, ...] = (
    "task_id", "agent_id", "status", "summary", "findings", "files_read",
    "files_changed", "commands_run", "tool_calls", "tests", "evidence", "risks",
    "unresolved_items", "artifacts", "token_usage", "cost_estimate",
    "recommended_next_action",
)

RESULT_CRITICAL_FIELDS: tuple[str, ...] = (
    "summary", "files_changed", "evidence", "tests", "unresolved_items",
)


RESULT_LIST_FIELDS: tuple[str, ...] = (
    "findings", "files_read", "files_changed", "commands_run", "tool_calls",
    "tests", "evidence", "risks", "unresolved_items", "artifacts",
    "recommended_next_action",
)


@dataclass
class ResultPackage:
    payload: dict[str, Any]

    @staticmethod
    def normalize(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Coerce common LLM scalar/object deviations into the strict schema.

        Models frequently emit ``token_usage: 0`` or a string instead of a
        one-item array. The control plane records the normalization so it is
        visible in audit/bus, but keeps missing/unknown/status checks strict.
        """
        out = dict(data)
        changed: list[str] = []
        for key in RESULT_LIST_FIELDS:
            value = out.get(key)
            if value is None:
                out[key] = []
                changed.append(key)
            elif not isinstance(value, list):
                out[key] = [value]
                changed.append(key)
        for key, default in (("token_usage", {}), ("cost_estimate", {})):
            value = out.get(key)
            if value is None:
                out[key] = dict(default)
                changed.append(key)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                out[key] = {"total": value} if key == "token_usage" else {
                    "amount": value, "currency": "USD", "estimated": True,
                }
                changed.append(key)
        return out, changed

    @classmethod
    def validate(cls, data: Any, *, expected_task_id: str = "", expected_agent_id: str = "") -> "ResultPackage":
        if not isinstance(data, dict):
            raise ValidationError("成果包必须是 JSON 对象")
        missing = [k for k in RESULT_REQUIRED_FIELDS if k not in data]
        if missing:
            raise ValidationError(f"成果包缺少字段: {missing}", details={"missing": missing})
        extra = set(data) - set(RESULT_REQUIRED_FIELDS)
        if extra:
            raise ValidationError(f"成果包包含未知字段: {sorted(extra)}", details={"extra": sorted(extra)})
        if data.get("status") not in ("completed", "partial", "failed"):
            raise ValidationError("成果包 status 必须是 completed/partial/failed")
        for key in ("summary",):
            _require_type(data[key], (str,), key)
        for key in ("findings", "files_read", "files_changed", "commands_run", "tool_calls",
                    "tests", "evidence", "risks", "unresolved_items", "artifacts",
                    "recommended_next_action"):
            if not isinstance(data[key], list):
                raise ValidationError(f"成果包 {key} 必须是数组")
        if not isinstance(data["token_usage"], dict):
            raise ValidationError("成果包 token_usage 必须是对象")
        if not isinstance(data["cost_estimate"], dict):
            raise ValidationError("成果包 cost_estimate 必须是对象")
        if not str(data["summary"]).strip():
            raise ValidationError("成果包 summary 不能为空")
        if expected_task_id and str(data["task_id"]) != expected_task_id:
            raise ValidationError(
                f"成果包 task_id 不匹配：期望 {expected_task_id}，实际 {data['task_id']}"
            )
        if expected_agent_id and str(data["agent_id"]) != expected_agent_id:
            raise ValidationError(
                f"成果包 agent_id 不匹配：期望 {expected_agent_id}，实际 {data['agent_id']}"
            )
        return cls(dict(data))

    @property
    def status(self) -> str:
        return str(self.payload.get("status"))

    @property
    def summary(self) -> str:
        return str(self.payload.get("summary", ""))

    @property
    def files_changed(self) -> list[str]:
        value = self.payload.get("files_changed")
        return [str(v) for v in value] if isinstance(value, list) else []

    @property
    def findings(self) -> list[Any]:
        value = self.payload.get("findings")
        return list(value) if isinstance(value, list) else []

    @property
    def unresolved_items(self) -> list[Any]:
        value = self.payload.get("unresolved_items")
        return list(value) if isinstance(value, list) else []

    @property
    def token_usage(self) -> dict[str, Any]:
        value = self.payload.get("token_usage")
        return dict(value) if isinstance(value, dict) else {}

    def digest(self) -> dict[str, Any]:
        """Result-sync digest: what another agent needs, not the full context.

        This is the AgentMesh cost model: a child can read 100 files, but only
        this compact result travels to the main node or a dependent task.
        """
        return {
            "task_id": self.payload.get("task_id"),
            "agent_id": self.payload.get("agent_id"),
            "status": self.payload.get("status"),
            "summary": str(self.payload.get("summary") or "")[:500],
            "findings": self.findings[:20],
            "files_changed": self.files_changed[:50],
            "tests": list(self.payload["tests"])[:20]
            if isinstance(self.payload.get("tests"), list) else [],
            "risks": list(self.payload["risks"])[:10]
            if isinstance(self.payload.get("risks"), list) else [],
            "unresolved_items": self.unresolved_items[:10],
            "token_usage": self.token_usage,
            "recommended_next_action": list(self.payload["recommended_next_action"])[:10]
            if isinstance(self.payload.get("recommended_next_action"), list) else [],
        }

    def compact(self) -> dict[str, Any]:
        """The minimal package the main node is allowed to read by default."""
        return {
            "task_id": self.payload.get("task_id"),
            "agent_id": self.payload.get("agent_id"),
            "status": self.payload.get("status"),
            "summary": self.payload.get("summary"),
            "findings": self.payload.get("findings", [])[:50],
            "files_changed": self.payload.get("files_changed", []),
            "tests": self.payload.get("tests", []),
            "evidence": self.payload.get("evidence", [])[:100],
            "risks": self.payload.get("risks", []),
            "unresolved_items": self.payload.get("unresolved_items", []),
            "token_usage": self.payload.get("token_usage", {}),
            "recommended_next_action": self.payload.get("recommended_next_action", []),
        }


VERIFICATION_REQUIRED_FIELDS: tuple[str, ...] = (
    "task_id", "verifier_agent", "verification_status", "checks", "warnings",
)


@dataclass
class VerificationReport:
    task_id: str
    verifier_agent: str
    status: str
    checks: dict[str, str]
    warnings: list[str] = field(default_factory=list)
    summary: str = ""
    evidence: list[Any] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    source: str = "control_plane"

    @classmethod
    def validate(cls, data: Any, *, expected_task_id: str = "", executor_agent: str = "") -> "VerificationReport":
        if not isinstance(data, dict):
            raise ValidationError("验证报告必须是 JSON 对象")
        missing = [k for k in VERIFICATION_REQUIRED_FIELDS if k not in data]
        if missing:
            raise ValidationError(f"验证报告缺少字段: {missing}")
        unknown = set(data) - set(VERIFICATION_REQUIRED_FIELDS) - {"summary", "evidence", "created_at", "source"}
        if unknown:
            raise ValidationError(f"验证报告包含未知字段: {sorted(unknown)}")
        status = str(data.get("verification_status"))
        if status not in ("passed", "failed", "warning", "skipped"):
            raise ValidationError("verification_status 必须是 passed/failed/warning/skipped")
        if not isinstance(data.get("checks"), dict):
            raise ValidationError("checks 必须是对象")
        warnings = data.get("warnings")
        if not isinstance(warnings, list):
            raise ValidationError("warnings 必须是数组")
        if expected_task_id and str(data.get("task_id")) != expected_task_id:
            raise ValidationError("验证报告 task_id 不匹配")
        verifier = str(data.get("verifier_agent") or "")
        if not verifier:
            raise ValidationError("验证报告 verifier_agent 不能为空")
        if executor_agent and verifier == executor_agent:
            raise ValidationError("执行 Agent 不能作为该任务的唯一验收者")
        return cls(
            task_id=str(data["task_id"]), verifier_agent=verifier, status=status,
            checks={str(k): str(v) for k, v in data["checks"].items()},
            warnings=[str(w) for w in warnings],
            summary=str(data.get("summary", "")),
            evidence=list(data.get("evidence", []) or []),
            created_at=str(data.get("created_at") or now_iso()),
            source=str(data.get("source") or "agent"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "verifier_agent": self.verifier_agent,
            "verification_status": self.status, "checks": self.checks,
            "warnings": self.warnings, "summary": self.summary,
            "evidence": self.evidence, "created_at": self.created_at,
            "source": self.source,
        }


@dataclass
class Approval:
    approval_id: str
    project_id: str
    kind: str
    subject: str
    details: dict[str, Any] = field(default_factory=dict)
    status: str = ApprovalStatus.PENDING.value
    plan_id: str = ""
    run_id: str = ""
    task_uid: str = ""
    requested_at: str = field(default_factory=now_iso)
    decided_at: str = ""
    decided_by: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id, "project_id": self.project_id,
            "kind": self.kind, "subject": self.subject, "details": self.details,
            "status": self.status, "plan_id": self.plan_id, "run_id": self.run_id,
            "task_uid": self.task_uid, "requested_at": self.requested_at,
            "decided_at": self.decided_at, "decided_by": self.decided_by,
            "note": self.note,
        }


@dataclass
class UsageRecord:
    record_id: str
    project_id: str
    agent_id: str
    model: str
    task_uid: str = ""
    run_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    tool_calls: int = 0
    retries: int = 0
    duration_ms: int = 0
    cost: float = 0.0
    estimated: bool = False
    status: str = "ok"
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id, "project_id": self.project_id,
            "agent_id": self.agent_id, "model": self.model, "task_uid": self.task_uid,
            "run_id": self.run_id, "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens, "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens, "total_tokens": self.total_tokens,
            "tool_calls": self.tool_calls, "retries": self.retries,
            "duration_ms": self.duration_ms, "cost": self.cost,
            "estimated": self.estimated, "status": self.status,
            "created_at": self.created_at,
        }


@dataclass
class ChangeRecord:
    change_id: str
    project_id: str
    task_uid: str
    agent_id: str
    base_commit: str
    base_hash: str
    before_hash: str
    after_hash: str
    files: list[str]
    patch_path: str = ""
    status: str = ChangeStatus.PROPOSED.value
    created_at: str = field(default_factory=now_iso)
    applied_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id, "project_id": self.project_id,
            "task_uid": self.task_uid, "agent_id": self.agent_id,
            "base_commit": self.base_commit, "base_hash": self.base_hash,
            "before_hash": self.before_hash, "after_hash": self.after_hash,
            "files": self.files, "patch_path": self.patch_path,
            "status": self.status, "created_at": self.created_at,
            "applied_at": self.applied_at,
        }


@dataclass
class MemoryRecord:
    memory_id: str
    project_id: str
    type: str
    title: str
    body: str
    source: str = ""
    confidence: float = 0.8
    scope: str = ""
    task_uid: str = ""
    expires_at: str = ""
    invalid_conditions: list[str] = field(default_factory=list)
    last_verified_at: str = ""
    injectable: bool = True
    status: str = MemoryStatus.ACTIVE.value
    conflict_group: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def validate(self) -> None:
        self.type = normalize_memory_type(self.type)
        if self.type not in ALL_MEMORY_TYPES:
            raise ValidationError(f"未知记忆类型 {self.type}")
        if not self.title.strip() or not self.body.strip():
            raise ValidationError("记忆 title/body 不能为空")
        if not (0.0 <= float(self.confidence) <= 1.0):
            raise ValidationError("置信度必须在 0..1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id, "project_id": self.project_id,
            "type": self.type, "title": self.title, "body": self.body,
            "source": self.source, "confidence": self.confidence,
            "scope": self.scope, "task_uid": self.task_uid, "expires_at": self.expires_at,
            "invalid_conditions": self.invalid_conditions,
            "last_verified_at": self.last_verified_at, "injectable": self.injectable,
            "status": self.status, "conflict_group": self.conflict_group,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


@dataclass
class ConversationRecord:
    conversation_id: str
    project_id: str
    agent_id: str
    session_id: str
    source_path: str
    title: str = ""
    cwd: str = ""
    started_at: str = ""
    ended_at: str = ""
    message_count: int = 0
    token_usage: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    raw_path: str = ""
    imported_at: str = field(default_factory=now_iso)
    fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id, "project_id": self.project_id,
            "agent_id": self.agent_id, "session_id": self.session_id,
            "source_path": self.source_path, "title": self.title, "cwd": self.cwd,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "message_count": self.message_count, "token_usage": self.token_usage,
            "summary": self.summary, "raw_path": self.raw_path,
            "imported_at": self.imported_at, "fingerprint": self.fingerprint,
        }


@dataclass
class DecisionRecord:
    decision_id: str
    project_id: str
    content: str
    agent_id: str = ""
    model: str = ""
    task_uid: str = ""
    evidence: list[Any] = field(default_factory=list)
    approved: bool = False
    approval_id: str = ""
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id, "project_id": self.project_id,
            "content": self.content, "agent_id": self.agent_id, "model": self.model,
            "task_uid": self.task_uid, "evidence": self.evidence,
            "approved": self.approved, "approval_id": self.approval_id,
            "created_at": self.created_at,
        }


@dataclass
class Checkpoint:
    checkpoint_id: str
    project_id: str
    kind: str
    snapshot: dict[str, Any]
    run_id: str = ""
    task_uid: str = ""
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id, "project_id": self.project_id,
            "kind": self.kind, "snapshot": self.snapshot, "run_id": self.run_id,
            "task_uid": self.task_uid, "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Plan helpers
# ---------------------------------------------------------------------------

def plan_from_mapping(data: dict[str, Any], *, project_id: str) -> TaskPlan:
    return TaskPlan.from_dict(data, project_id=project_id)


def stable_plan_hash(plan: TaskPlan) -> str:
    return sha1_hex(stable_json(plan.to_dict()))


def files_in_scope(files: Iterable[str], scope: list[str], out_of_scope: list[str]) -> tuple[list[str], list[str]]:
    """Return (allowed, violations). Empty scope means no restriction."""
    allowed: list[str] = []
    violations: list[str] = []
    for raw in files:
        rel = normalize_scope_path(raw)
        if any(scope_matches(p, rel) for p in out_of_scope):
            violations.append(rel)
            continue
        if not scope or any(scope_matches(p, rel) for p in scope):
            allowed.append(rel)
        else:
            violations.append(rel)
    return allowed, violations


def pattern_overlap(a: list[str], b: list[str]) -> list[str]:
    """Best-effort overlap between two scope pattern sets.

    Exact paths and prefix patterns are compared deterministically; pure
    wildcards that cannot be decided statically are treated as potentially
    overlapping only when the literal prefixes intersect.
    """
    overlaps: list[str] = []
    for pa in a:
        for pb in b:
            na, nb = normalize_scope_path(pa), normalize_scope_path(pb)
            la = _literal_prefix(na)
            lb = _literal_prefix(nb)
            if la == lb or la.startswith(lb + "/") or lb.startswith(la + "/") or not la or not lb:
                if na == nb or la == lb:
                    overlaps.append(na)
                elif la.startswith(lb + "/") or lb.startswith(la + "/"):
                    overlaps.append(na)
    return sorted(set(overlaps))


def _literal_prefix(pattern: str) -> str:
    for i, ch in enumerate(pattern):
        if ch in "*?[":
            return pattern[:i].rstrip("/")
    return pattern.rstrip("/")


def file_matches_any(path: str, patterns: Iterable[str]) -> bool:
    rel = normalize_scope_path(path)
    return any(fnmatch.fnmatch(rel, normalize_scope_path(p)) or scope_matches(p, rel) for p in patterns)
