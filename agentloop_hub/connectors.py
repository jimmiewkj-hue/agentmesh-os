"""Agent Connector: the unified, capability-first view of every local agent.

需求整理-v1 模块 1 要求所有异构 Agent 被降级为同一资源池，统一抽象至少包含
``{"agent_id": ..., "capability": [...]}``。本模块负责：

* 定义能力词表与任务类型到能力需求的映射；
* 把 ``AgentRecord`` 的多布尔能力字段归一化为能力列表；
* 生成对外 Connector Manifest（CLI/HTTP/Web 共用）；
* 为新接入产品提供确定性的本地探测画像（存在才注册，不虚构）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .models import AgentRecord, TaskSpec, TaskType


CAPABILITY_VOCABULARY: dict[str, str] = {
    "read_files": "读取项目文件",
    "write_files": "写入/修改项目文件",
    "terminal": "执行本地命令与测试",
    "web_search": "联网检索",
    "mcp": "支持 MCP 工具协议",
    "session_read": "可读取本地历史会话",
    "session_write": "可写入/续写本地会话",
    "structured_output": "可稳定产出结构化成果包",
    "streaming": "流式输出",
    "background": "可后台非交互执行",
    "code_generation": "代码生成与实现",
    "code_review": "代码审查与缺陷定位",
    "architecture": "架构设计与方案评审",
    "testing": "测试编写与执行",
    "documentation": "文档编写与整理",
    "analysis": "结构化分析与大规模扫描",
    "execution": "通用任务执行",
    "local_mcp": "作为本地 MCP 服务被调用",
    "auto_executable": "无需人工操作即可自动执行任务",
    "gui_control": "可通过 GUI 自动化驱动（窗口/输入/会话回读）",
}

# 需求整理-v1 模块 1 的 11 类产品（Trae/Qoder 各含国际版与 CN 版）。
AGENT_CLASSES: tuple[str, ...] = (
    "trae", "qoder", "workbuddy", "opencode", "zcode", "dsh",
    "cursor", "claude_code", "gemini_cli", "codex_cli", "aider",
)

TASK_CAPABILITY_REQUIREMENTS: dict[str, list[str]] = {
    TaskType.ANALYSIS.value: ["read_files", "analysis"],
    TaskType.REVIEW.value: ["read_files", "code_review"],
    TaskType.IMPLEMENTATION.value: ["read_files", "write_files", "terminal", "code_generation"],
    TaskType.DOCUMENTATION.value: ["read_files", "write_files", "documentation"],
    TaskType.VERIFICATION.value: ["read_files", "terminal", "testing", "structured_output"],
    TaskType.ARCHITECTURE.value: ["read_files", "architecture", "code_review"],
}


@dataclass(frozen=True)
class ConnectorProfile:
    """How a product is discovered and which capabilities it is assumed to have.

    ``assumed`` is only applied when the product is actually installed; the
    discovery layer never marks an absent executable as available.
    """

    agent_id: str
    product: str
    edition: str
    assumed: tuple[str, ...]
    access_level_if_installed: str = "L2"
    adapter_type: str = "file_relay"
    cli_names: tuple[str, ...] = ()
    cli_args: tuple[str, ...] = ()
    prompt_mode: str = "arg"
    note: str = ""


# Twelve products named by 需求整理-v1. Trae/Qoder/WorkBuddy/OpenCode/ZCode/DSH
# keep their verified session readers in discovery.py; the five CLI/editor entries
# below are only registered when a real executable/config directory exists.
CONNECTOR_PROFILES: tuple[ConnectorProfile, ...] = (
    ConnectorProfile("trae", "Trae", "international", ("read_files", "write_files", "terminal", "mcp"), "L2"),
    ConnectorProfile("trae_cn", "Trae CN", "domestic", ("read_files", "write_files", "terminal", "mcp"), "L2"),
    ConnectorProfile("qoder", "Qoder", "international", ("read_files", "write_files", "terminal", "mcp"), "L2"),
    ConnectorProfile("qoder_cn", "Qoder CN", "domestic", ("read_files", "write_files", "terminal", "mcp"), "L2"),
    ConnectorProfile("workbuddy", "WorkBuddy", "domestic", ("read_files", "write_files", "terminal", "mcp"), "L2"),
    ConnectorProfile("zcode", "ZCode", "domestic", ("read_files", "write_files", "terminal", "mcp"), "L2"),
    ConnectorProfile("dsh", "DeepSeek Harness", "domestic", ("read_files", "write_files", "terminal", "mcp"), "L2"),
    ConnectorProfile("opencode", "OpenCode", "international", ("read_files", "write_files", "terminal", "mcp"), "L3", "opencode"),
    ConnectorProfile(
        "claude_code", "Claude Code", "international",
        ("read_files", "write_files", "terminal", "code_generation", "code_review", "architecture"),
        "L3", "cli", cli_names=("claude",), cli_args=("-p",), prompt_mode="arg",
        note="Claude Code CLI 非交互模式；需用户自行完成登录/授权",
    ),
    ConnectorProfile(
        "gemini_cli", "Gemini CLI", "international",
        ("read_files", "write_files", "terminal", "web_search", "code_generation"),
        "L3", "cli", cli_names=("gemini",), cli_args=("-p",), prompt_mode="arg",
        note="Gemini CLI 非交互模式；联网能力取决于账号与配置",
    ),
    ConnectorProfile(
        "codex_cli", "Codex CLI", "international",
        ("read_files", "write_files", "terminal", "code_generation", "testing"),
        "L3", "cli", cli_names=("codex",), cli_args=("exec",), prompt_mode="arg",
        note="Codex CLI 非交互模式；需用户自行完成登录",
    ),
    ConnectorProfile(
        "cursor", "Cursor", "international",
        ("read_files", "write_files", "terminal", "mcp"), "L1", "file_relay",
        note="GUI 编辑器，文件接力；不读取其私有会话状态",
    ),
    ConnectorProfile(
        "aider", "Aider", "international",
        ("read_files", "write_files", "terminal", "code_generation", "testing"),
        "L3", "cli", cli_names=("aider",), cli_args=("--yes-always", "--message"), prompt_mode="arg",
        note="Aider CLI；需要用户在项目内配置模型密钥",
    ),
    ConnectorProfile(
        "rea", "REA Studio", "local",
        ("local_mcp", "mcp", "read_files", "analysis", "code_review"),
        "L2", "mcp",
        note="本地逆向工程 MCP 服务；通过真实 MCP 协议列出并调用工具，不虚构参数",
    ),
)


def profile_for(agent_id: str) -> ConnectorProfile | None:
    return next((p for p in CONNECTOR_PROFILES if p.agent_id == agent_id), None)


def is_auto_executable(agent: AgentRecord) -> bool:
    """Whether this Agent can execute a task without a human in a GUI.

    File relay / manual / MCP Agents are intentionally excluded. A project can
    opt one relay Agent in explicitly via ``extra.allow_auto_relay`` or the
    router's ``router_allow_manual_relay`` switch.

    ``gui_automation`` is a capability, not a name-based bypass: it requires an
    online agent with a known GUI app profile (window title + transcript reader).
    """
    kind = (agent.adapter_type or "").lower()
    explicit = (agent.capabilities or {}).get("auto_executable")
    if explicit is False:
        return False
    if explicit is True:
        return agent.status not in ("offline", "error")
    if kind in ("cli", "opencode"):
        if agent.status in ("offline", "error"):
            return False
        config = agent.adapter_config or {}
        command = config.get("command") or []
        executable = str(config.get("executable") or "")
        # CLI is non-interactive by nature, but "type only" is not evidence:
        # there must be a configured command/executable.
        return bool(executable or command)
    if kind == "gui_automation":
        config = agent.adapter_config or {}
        return bool(config.get("gui_app")) and agent.status not in ("offline", "error", "unknown")
    if kind == "file_relay":
        return bool((agent.extra or {}).get("allow_auto_relay"))
    return False


def derive_capability_list(agent: AgentRecord) -> list[str]:
    """Normalize an AgentRecord to a stable capability list.

    Explicit capability keys win. Product-level assumptions are only added when
    the executable/store is actually present (status != offline).
    """
    capabilities = agent.capabilities or {}
    caps: set[str] = {name for name, enabled in capabilities.items() if enabled}
    explicitly_disabled = {name for name, enabled in capabilities.items()
                           if enabled is False}
    installed = agent.status not in ("offline", "unknown") or bool(
        (agent.adapter_config or {}).get("executable")
    )
    if installed:
        profile = profile_for(agent.product.lower().replace(" ", "_")) or profile_for(agent.agent_id)
        if profile:
            # Product assumptions never override an explicit user/declared false.
            caps.update(set(profile.assumed) - explicitly_disabled)
    caps -= explicitly_disabled
    # Analysis requires a non-interactive CLI or a verified GUI automation
    # bridge. A file-relay GUI alone still needs a human and is excluded.
    if "read_files" in caps and agent.adapter_type in ("cli", "opencode", "gui_automation"):
        caps.add("analysis")
    if "write_files" in caps:
        caps.update({"code_generation", "documentation"})
    if "terminal" in caps:
        caps.update({"testing", "execution"})
    # Any coding-capable local agent can review and design; router still ranks
    # by measured success/cost/speed rather than assuming equal quality.
    if {"read_files", "write_files", "terminal"}.issubset(caps):
        caps.update({"code_review", "architecture"})
    if "structured_output" in caps:
        caps.add("structured_output")
    if (agent.adapter_config or {}).get("mode") == "mcp":
        caps.add("local_mcp")
    if agent.adapter_type == "gui_automation":
        caps.add("gui_control")
    if is_auto_executable(agent):
        caps.add("auto_executable")
    return sorted(c for c in caps if c in CAPABILITY_VOCABULARY)


def required_capabilities_for(spec: TaskSpec) -> list[str]:
    required = set(TASK_CAPABILITY_REQUIREMENTS.get(spec.type, ["read_files"]))
    required.update(spec.required_capabilities or [])
    return sorted(required)


def capability_evidence(agent: AgentRecord) -> dict[str, dict[str, Any]]:
    """Per-capability evidence: declared / derived / disabled / probed.

    A boolean list alone cannot distinguish an explicit user disable from a
    product assumption or a real probe.
    """
    declared = dict(agent.capabilities or {})
    evidence: dict[str, dict[str, Any]] = {}
    profile = profile_for(agent.product.lower().replace(" ", "_")) or profile_for(agent.agent_id)
    installed = agent.status not in ("offline", "unknown") or bool(
        (agent.adapter_config or {}).get("executable"))
    if profile and installed:
        for name in profile.assumed:
            evidence.setdefault(name, {"status": "declared",
                                       "source": f"product_profile:{profile.agent_id}"})
    for name, enabled in declared.items():
        evidence[name] = {
            "status": "declared" if enabled else "disabled",
            "source": "agent.capabilities",
        }
    for name in derive_capability_list(agent):
        evidence.setdefault(name, {"status": "derived", "source": "capability_rules"})
    auto_ok = is_auto_executable(agent)
    if not auto_ok:
        auto_status = "unavailable"
    elif str(agent.last_error or "").strip():
        auto_status = "degraded"
    else:
        auto_status = "probed"
    evidence["auto_executable"] = {
        "status": auto_status,
        "source": "adapter_config+status",
        "adapter_type": agent.adapter_type,
        "version": agent.version,
        "checked_at": agent.last_seen,
        "last_error": str(agent.last_error or "")[:300],
    }
    return evidence


def agent_descriptor(agent: AgentRecord) -> dict[str, Any]:
    """Unified connector record: ``agent_id`` + ``capability`` list."""
    return {
        "agent_id": agent.agent_id,
        "name": agent.name,
        "product": agent.product,
        "edition": agent.edition,
        "version": agent.version,
        "status": agent.status,
        "access_level": agent.access_level,
        "adapter_type": agent.adapter_type,
        "capability": derive_capability_list(agent),
        "models": list(agent.models),
        "sandbox": bool((agent.permissions or {}).get("allowed_roots")),
        "auto_executable": is_auto_executable(agent),
        "capability_evidence": capability_evidence(agent),
        "execution_mode": agent.adapter_type,
    }


def connector_manifest(agents: Iterable[AgentRecord]) -> dict[str, Any]:
    return {
        "protocol": "agentmesh.connector.v1",
        "capability_vocabulary": CAPABILITY_VOCABULARY,
        "agents": [agent_descriptor(a) for a in agents],
        "task_requirements": TASK_CAPABILITY_REQUIREMENTS,
    }


def missing_capabilities(agent: AgentRecord, required: Iterable[str]) -> list[str]:
    available = set(derive_capability_list(agent))
    return sorted(set(required) - available)


__all__ = [
    "CAPABILITY_VOCABULARY", "TASK_CAPABILITY_REQUIREMENTS", "CONNECTOR_PROFILES",
    "AGENT_CLASSES", "ConnectorProfile", "profile_for", "derive_capability_list",
    "required_capabilities_for", "agent_descriptor", "connector_manifest",
    "missing_capabilities", "is_auto_executable",
]
