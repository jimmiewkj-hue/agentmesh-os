"""Adapter contract shared by CLI, OpenCode, file-relay and manual relay."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..models import AgentRecord, Task


@dataclass
class ExecutionUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    tool_calls: int = 0
    estimated: bool = False

    def normalized(self) -> "ExecutionUsage":
        if not self.total_tokens:
            self.total_tokens = self.input_tokens + self.output_tokens
        return self

    def to_dict(self) -> dict[str, Any]:
        self.normalized()
        return {
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens, "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens, "cost": self.cost,
            "tool_calls": self.tool_calls, "estimated": self.estimated,
        }


@dataclass
class ExecutionRequest:
    """One adapter invocation.

    `purpose` is one of plan/task/verify/summary. It only affects the prompt the
    control plane generates; every purpose uses the same result-file contract.
    """

    project_id: str
    project_root: str
    task: Task
    workdir: str
    task_dir: str
    prompt_path: str
    result_path: str
    model: str = ""
    context_length: int = 0
    capability_level: str = ""
    timeout_seconds: int = 1800
    purpose: str = "task"
    # When true, GUI adapters switch the target app to its own full-access
    # mode and auto-accept residual permission dialogs via the renderer DOM.
    full_control: bool = True
    env: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Runner-supplied callback.  GUI adapters call it when a node needs a
    # human (manual submit / app restart) so the WebUI can block the chain and
    # show instructions before the task returns.
    notify: Any = None
    # Set by Runner.cancel(): adapters/readers must stop waiting promptly.
    cancel_event: Any = None


@dataclass
class ExecutionOutcome:
    status: str  # completed | waiting_user | failed | timeout | canceled
    text: str = ""
    usage: ExecutionUsage = field(default_factory=ExecutionUsage)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    commands: list[dict[str, Any]] = field(default_factory=list)
    result_payload: dict[str, Any] | None = None
    result_path: str = ""
    logs: str = ""
    error: str = ""
    session_id: str = ""
    duration_ms: int = 0
    pid: int = 0
    extras: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "text": self.text, "usage": self.usage.to_dict(),
            "tool_calls": self.tool_calls, "commands": self.commands,
            "result_payload": self.result_payload, "result_path": self.result_path,
            "logs": self.logs, "error": self.error, "session_id": self.session_id,
            "duration_ms": self.duration_ms, "pid": self.pid, "extras": self.extras,
            "error_code": self.error_code,
        }


class AgentAdapter(Protocol):
    agent: AgentRecord

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome: ...

    def health(self) -> dict[str, Any]: ...


class BaseAdapter:
    def __init__(self, agent: AgentRecord, config: dict[str, Any] | None = None):
        self.agent = agent
        self.config = dict(config or agent.adapter_config or {})

    def health(self) -> dict[str, Any]:
        return {"agent_id": self.agent.agent_id, "status": self.agent.status,
                "adapter_type": self.agent.adapter_type}
