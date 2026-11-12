"""MCP server: memory pull, search and web tools over the real MCP protocol."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from hub_test_support import ROOT, make_project

from agentloop_hub.control import tools as tools_mod
from agentloop_hub.mcp_server import build_tools


def test_memory_tools_pull_from_real_hub(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    tools = build_tools(hub)
    hub.memory_add(type="knowledge", title="vite 启动失败", body="node 版本不兼容")
    found = tools["memory_search"]("vite")
    assert any("vite" in str(item.get("title", "")).lower()
               or "vite" in str(item.get("body", "")).lower() for item in found)
    listed = tools["memory_list"](type="knowledge", limit=10)
    assert listed
    bootstrap = tools["memory_bootstrap"]()
    assert isinstance(bootstrap, dict) and bootstrap
    state = tools["task_state"]()
    assert "run" in state and "tasks" in state
    hub.close()


def test_bus_events_tool_reads_append_only_stream(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    hub.bus_publish("message", {"note": "hello"}, source="test")
    out = build_tools(hub)["bus_events"](after_seq=0, limit=10)
    assert out["count"] >= 1
    assert out["last_seq"] >= out["events"][0]["seq"]
    hub.close()


def test_web_search_tool_delegates_to_control_plane(tmp_path, monkeypatch):
    repo, hub, _ = make_project(tmp_path)
    monkeypatch.setattr(tools_mod, "web_search",
                        lambda q, **k: {"query": q, "results": [{"title": "T", "url": "u"}],
                                        "error": ""})
    out = build_tools(hub)["web_search"]("hello", max_results=3)
    assert out["query"] == "hello" and out["results"]
    hub.close()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def test_mcp_stdio_end_to_end(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    repo = _make_repo(tmp_path)
    env = dict(os.environ)
    env["AGENTLOOP_HOME"] = str(tmp_path / "home")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agentloop_hub", "mcp-serve", "--project", str(repo)],
        env=env, cwd=str(ROOT),
    )

    async def run():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                result = await session.call_tool("memory_bootstrap", {"agent_id": ""})
                return names, result

    names, result = asyncio.run(run())
    assert {"memory_bootstrap", "memory_search", "memory_list",
            "web_search", "task_state", "bus_events"} <= names
    assert result.isError is False
    assert result.content