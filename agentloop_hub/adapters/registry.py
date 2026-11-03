"""Adapter registry: maps a registered AgentRecord to its adapter implementation."""

from __future__ import annotations

from ..errors import AdapterError
from ..models import AdapterType, AgentRecord
from .base import AgentAdapter
from .cli import CliAgentAdapter
from .gui import GuiAutomationAdapter
from .mcp import McpAdapter
from .opencode import OpenCodeAdapter
from .relay import FileRelayAdapter, ManualAdapter


def create_adapter(agent: AgentRecord) -> AgentAdapter:
    kind = agent.adapter_type
    if kind == AdapterType.OPENCODE.value:
        return OpenCodeAdapter(agent)
    if kind == AdapterType.MCP.value:
        return McpAdapter(agent)
    if kind == AdapterType.CLI.value:
        return CliAgentAdapter(agent)
    if kind == AdapterType.GUI_AUTOMATION.value:
        return GuiAutomationAdapter(agent)
    if kind == AdapterType.FILE_RELAY.value:
        return FileRelayAdapter(agent)
    if kind == AdapterType.MANUAL.value:
        return ManualAdapter(agent)
    if kind == AdapterType.SESSION_ONLY.value:
        raise AdapterError(f"Agent {agent.agent_id} 仅支持会话读取，不能执行任务")
    raise AdapterError(f"未知适配器类型: {kind}")
