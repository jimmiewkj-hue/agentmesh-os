"""Agent adapters."""

from .base import AgentAdapter, BaseAdapter, ExecutionOutcome, ExecutionRequest, ExecutionUsage
from .cli import CliAgentAdapter
from .discovery import DiscoveryContext, discover_agents, detect_models_for_agent
from .dsh import DshAdapter
from .gui import GuiAutomationAdapter
from .mcp import McpAdapter, McpSession
from .opencode import OpenCodeAdapter
from .registry import create_adapter
from .relay import FileRelayAdapter, ManualAdapter
from .zcode import ZcodeAdapter

__all__ = [
    "AgentAdapter", "BaseAdapter", "ExecutionOutcome", "ExecutionRequest", "ExecutionUsage",
    "CliAgentAdapter", "DiscoveryContext", "detect_models_for_agent", "discover_agents",
    "DshAdapter", "GuiAutomationAdapter", "McpAdapter", "McpSession", "OpenCodeAdapter", "ZcodeAdapter",
    "create_adapter", "FileRelayAdapter", "ManualAdapter",
]
