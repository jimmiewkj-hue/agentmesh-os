"""Hub configuration: deterministic defaults, project overrides, and data paths."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import read_json

DEFAULT_DANGEROUS_PATTERNS: list[str] = [
    r"rm\s+-rf",
    r"rmdir\s+/s",
    r"del\s+/f",
    r"format\s+[a-z]:",
    r"git\s+reset\s+--hard",
    r"git\s+clean\s+-[a-z]*f",
    r"git\s+push\s+--force",
    r"git\s+push\s+-f",
    r"npm\s+publish",
    r"pnpm\s+publish",
    r"yarn\s+publish",
    r"kubectl\s+(apply|delete)",
    r"docker\s+(rm|rmi|system\s+prune)",
    r"shutdown",
    r"reg\s+delete",
    r"drop\s+(table|database)",
    r"truncate\s+table",
]

DEFAULT_HIGH_RISK_FILE_PATTERNS: list[str] = [
    "**/package-lock.json",
    "**/pnpm-lock.yaml",
    "**/yarn.lock",
    "**/poetry.lock",
    "**/Pipfile.lock",
    "**/go.sum",
    "**/.env",
    "**/.env.*",
    "**/id_rsa*",
    "**/*.pem",
    "**/*.key",
]


@dataclass
class HubConfig:
    """Effective configuration. All values are deterministic and inspectable."""

    max_parallel: int = 2
    relay_poll_seconds: float = 1.0
    relay_timeout_seconds: int = 1800
    cli_timeout_seconds: int = 3600
    plan_timeout_seconds: int = 900
    summary_timeout_seconds: int = 600
    verify_timeout_seconds: int = 180
    verify_server_probe_seconds: int = 15
    # Repeated nodes for the same Agent continue that Agent's prior
    # conversation and worktree instead of starting a fresh one.
    chain_reuse_agent_sessions: bool = True

    default_task_token_budget: int = 30000
    default_project_token_budget: int = 0
    default_plan_token_budget: int = 40000
    budget_hard_stop: bool = False
    # Count only billable tokens (input/output/reasoning) against the quota;
    # cached tokens are reported separately and never inflate consumption.
    budget_billable_only: bool = True
    budget_warn_ratio: float = 0.8

    max_failures_before_circuit_break: int = 3
    max_retries_default: int = 2

    context_summary_tokens: int = 6000
    context_findings_tokens: int = 12000
    context_evidence_tokens: int = 24000
    context_raw_tokens: int = 60000

    # Context Bus
    bus_payload_max_tokens: int = 4000
    bus_retention_events: int = 50000

    # Unified memory bootstrap
    memory_bootstrap_limit: int = 8

    # Capability Router
    router_weights: dict[str, float] = field(default_factory=lambda: {
        "capability": 0.40, "success": 0.25, "verification": 0.15,
        "speed": 0.10, "cost": 0.10,
    })
    router_slow_reference_ms: int = 180000
    router_cost_reference_usd: float = 0.05
    router_exclude_main: bool = True
    # Automatic runs only dispatch Agents that can execute without a human
    # operating a GUI/MCP client. Explicitly enabling manual relay is opt-in.
    router_require_auto_executable: bool = True
    router_allow_manual_relay: bool = False
    # Untested Agents get a lower prior than an Agent with observed evidence;
    # small samples are shrunk toward the neutral prior instead of being
    # treated as fully reliable or fully failed.
    router_untested_prior: float = 0.4
    router_sample_strength: int = 3

    # Cost Optimizer
    semantic_cache_threshold: float = 0.82
    semantic_cache_max_entries: int = 200
    benchmark_min_tasks: int = 1

    audit_chain_enabled: bool = True
    auto_merge_commit: bool = False
    require_independent_verifier: bool = True
    verify_build: bool = True
    verifier_agent_id: str | None = None
    # Plans sometimes omit an entry file (e.g. src/main.js) that the task
    # objective requires.  "warn" keeps the task completed with a visible
    # warning; "strict" fails on any out-of-scope file.
    scope_violation_policy: str = "warn"

    # Programmatic GUI-agent interface. WorkBuddy's task deeplink uses
    # welcomeMode code/work/design; "code" is the development mode and maps
    # to source_mode "craft" in workbuddy.db. GUI clicks are only a fallback.
    workbuddy_welcome_mode: str = "code"
    workbuddy_permission_mode: str = "fullAccess"
    workbuddy_agent_mode: str = "craft"
    # User-facing "完全控制": when enabled, GUI agents are switched to their
    # own full-access mode and any residual permission dialog is accepted via
    # the agent's renderer interface (CDP DOM), never via OS input.
    full_control: bool = True
    # Multi-round continuation: when a round ends without satisfying the goal,
    # the main node plans the next minimal round instead of stopping. The cap
    # counts total rounds including the first one; repeated identical blockers
    # stop early. Modelled on DSH goal rounds and opencode's turn loop.
    multi_round: bool = True
    max_rounds: int = 3
    blocked_after_rounds: int = 2
    round_replan: bool = True
    workspace_verify: bool = True
    interface_restart_timeout_seconds: int = 60

    dangerous_command_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_DANGEROUS_PATTERNS))
    high_risk_file_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_HIGH_RISK_FILE_PATTERNS))

    excluded_paths: list[str] = field(default_factory=lambda: [
        ".git", ".agentloop", ".agentloop-task", "node_modules", ".venv", "venv",
        "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build", ".next",
        "target", ".idea", ".vscode", "*.log",
    ])

    # USD per 1M tokens; populated by the user. Unknown models cost 0.
    pricing: dict[str, dict[str, float]] = field(default_factory=dict)

    # Agent-specific overrides keyed by agent_id.
    agent_defaults: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_parallel": self.max_parallel,
            "relay_poll_seconds": self.relay_poll_seconds,
            "relay_timeout_seconds": self.relay_timeout_seconds,
            "cli_timeout_seconds": self.cli_timeout_seconds,
            "plan_timeout_seconds": self.plan_timeout_seconds,
            "summary_timeout_seconds": self.summary_timeout_seconds,
            "verify_timeout_seconds": self.verify_timeout_seconds,
            "verify_server_probe_seconds": self.verify_server_probe_seconds,
            "chain_reuse_agent_sessions": self.chain_reuse_agent_sessions,
            "default_task_token_budget": self.default_task_token_budget,
            "default_project_token_budget": self.default_project_token_budget,
            "default_plan_token_budget": self.default_plan_token_budget,
            "budget_hard_stop": self.budget_hard_stop,
            "budget_billable_only": self.budget_billable_only,
            "budget_warn_ratio": self.budget_warn_ratio,
            "max_failures_before_circuit_break": self.max_failures_before_circuit_break,
            "max_retries_default": self.max_retries_default,
            "context_summary_tokens": self.context_summary_tokens,
            "context_findings_tokens": self.context_findings_tokens,
            "context_evidence_tokens": self.context_evidence_tokens,
            "context_raw_tokens": self.context_raw_tokens,
            "bus_payload_max_tokens": self.bus_payload_max_tokens,
            "bus_retention_events": self.bus_retention_events,
            "memory_bootstrap_limit": self.memory_bootstrap_limit,
            "router_weights": self.router_weights,
            "router_slow_reference_ms": self.router_slow_reference_ms,
            "router_cost_reference_usd": self.router_cost_reference_usd,
            "router_exclude_main": self.router_exclude_main,
            "router_require_auto_executable": self.router_require_auto_executable,
            "router_allow_manual_relay": self.router_allow_manual_relay,
            "router_untested_prior": self.router_untested_prior,
            "router_sample_strength": self.router_sample_strength,
            "semantic_cache_threshold": self.semantic_cache_threshold,
            "semantic_cache_max_entries": self.semantic_cache_max_entries,
            "benchmark_min_tasks": self.benchmark_min_tasks,
            "audit_chain_enabled": self.audit_chain_enabled,
            "auto_merge_commit": self.auto_merge_commit,
            "require_independent_verifier": self.require_independent_verifier,
            "verify_build": self.verify_build,
            "verifier_agent_id": self.verifier_agent_id,
            "workbuddy_welcome_mode": self.workbuddy_welcome_mode,
            "workbuddy_permission_mode": self.workbuddy_permission_mode,
            "workbuddy_agent_mode": self.workbuddy_agent_mode,
            "full_control": self.full_control,
            "multi_round": self.multi_round,
            "max_rounds": self.max_rounds,
            "blocked_after_rounds": self.blocked_after_rounds,
            "round_replan": self.round_replan,
            "workspace_verify": self.workspace_verify,
            "interface_restart_timeout_seconds": self.interface_restart_timeout_seconds,
            "dangerous_command_patterns": self.dangerous_command_patterns,
            "high_risk_file_patterns": self.high_risk_file_patterns,
            "excluded_paths": self.excluded_paths,
            "pricing": self.pricing,
            "agent_defaults": self.agent_defaults,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HubConfig":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        cfg = cls()
        for key, value in (data or {}).items():
            if key in allowed and value is not None:
                setattr(cfg, key, value)
        return cfg

    def merged(self, overlay: dict[str, Any]) -> "HubConfig":
        base = self.to_dict()
        cfg = HubConfig.from_dict(_deep_merge(base, overlay or {}))
        return cfg

    def price_for(self, model: str) -> dict[str, float]:
        key = (model or "").strip()
        if not key:
            return {"input": 0.0, "output": 0.0}
        entry = self.pricing.get(key) or self.pricing.get(key.split("/")[-1]) or {}
        return {
            "input": float(entry.get("input", 0.0) or 0.0),
            "output": float(entry.get("output", 0.0) or 0.0),
        }


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def default_home() -> Path:
    env = os.environ.get("AGENTLOOP_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".agentloop"


def load_config(project_root: str | os.PathLike[str] | None = None) -> HubConfig:
    """Load defaults, then the global config, then the project config."""
    cfg = HubConfig()
    global_path = default_home() / "config.json"
    if global_path.exists():
        cfg = cfg.merged(read_json(global_path, {}) or {})
    if project_root:
        project_path = Path(project_root) / ".agentloop" / "config.json"
        if project_path.exists():
            cfg = cfg.merged(read_json(project_path, {}) or {})
    return cfg


def agent_registry_path() -> Path:
    return default_home() / "agents.json"


def save_config(config: HubConfig, path: str | os.PathLike[str]) -> None:
    from .util import write_json

    write_json(path, config.to_dict(), indent=2)


def config_example() -> dict[str, Any]:
    return HubConfig().to_dict()


__all__ = [
    "HubConfig",
    "load_config",
    "save_config",
    "default_home",
    "agent_registry_path",
    "config_example",
    "DEFAULT_DANGEROUS_PATTERNS",
    "DEFAULT_HIGH_RISK_FILE_PATTERNS",
]
