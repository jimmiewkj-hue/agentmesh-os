from __future__ import annotations

import sys
from pathlib import Path

from agentloop_hub.adapters.mcp import McpAdapter
from agentloop_hub.models import AgentRecord

MCP_SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"


def make_mcp_agent() -> AgentRecord:
    return AgentRecord(
        agent_id="mock_mcp", name="Mock MCP", product="Mock MCP",
        adapter_type="mcp", access_level="L2",
        adapter_config={
            "mode": "mcp", "command": sys.executable,
            "args": [str(MCP_SERVER)], "timeout_seconds": 10,
        },
        capabilities={"mcp": True, "local_mcp": True},
        models=[],
    )


def test_mcp_health_and_tools():
    adapter = McpAdapter(make_mcp_agent())
    health = adapter.health()
    assert health["available"] is True
    assert health["tool_count"] == 2
    assert health["server"]["name"] == "mock-mcp"
    tools = adapter.tools()
    assert [t["name"] for t in tools["tools"]] == ["add", "echo"]
    assert tools["tools"][0]["input_schema"]["required"] == ["a", "b"]


def test_mcp_tool_call():
    adapter = McpAdapter(make_mcp_agent())
    result = adapter.call("add", {"a": 2, "b": 3})
    assert result["tool"] == "add"
    text = result["result"]["content"][0]["text"]
    assert text == "5.0"
    echo = adapter.call("echo", {"message": "hello"})
    assert echo["result"]["content"][0]["text"] == "hello"
