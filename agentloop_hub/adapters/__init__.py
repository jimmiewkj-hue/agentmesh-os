"""Agent adapters."""

from .base import AgentAdapter, BaseAdapter, ExecutionOutcome, ExecutionRequest, ExecutionUsage
from .cli import CliAgentAdapter
from .discovery import DiscoveryContext, discover_agents, detect_models_for_agent
from .gui import GuiAutomationAdapter
from .mcp import McpAdapter, McpSession
from .opencode import OpenCodeAdapter
from .registry import create_adapter
from .relay import FileRelayAdapter, ManualAdapter

__all__ = [
    "AgentAdapter", "BaseAdapter", "ExecutionOutcome", "ExecutionRequest", "ExecutionUsage",
    "CliAgentAdapter", "DiscoveryContext", "detect_models_for_agent", "discover_agents",
    "GuiAutomationAdapter", "McpAdapter", "McpSession", "OpenCodeAdapter",
    "create_adapter", "FileRelayAdapter", "ManualAdapter",
]
