"""MCP server exposing AgentMesh memory and control-plane tools to agents.

External agents (Claude Code, OpenCode, Qoder, WorkBuddy, ...) connect over
stdio and can *pull* the unified memory instead of only receiving it through
prompt injection, and can call the Controller's keyless web search. This closes
the M2 pull gap and gives the Context Bus an MCP-facing surface.

Run:  python -m agentloop_hub mcp-serve --project <path>

stdout is the MCP channel; nothing else may be printed there.
"""

from __future__ import annotations

import os
from typing import Any

from .hub import Hub


def build_tools(hub: Hub) -> dict[str, Any]:
    """Tool callables bound to this project's Hub (also used by unit tests)."""

    def memory_bootstrap(agent_id: str = "", scope: str = "") -> dict[str, Any]:
        """Pull the unified-memory bootstrap (project state, main node, recent runs)."""
        return hub.bootstrap(agent_id=agent_id, scope=scope)

    def memory_search(keyword: str, limit: int = 50) -> list[dict[str, Any]]:
        """Search unified-memory entries by keyword."""
        results = hub.memory_search(keyword)
        return results[: max(1, int(limit))]

    def memory_list(type: str = "", status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        """List unified-memory entries, optionally filtered by type/status."""
        return hub.memory_list(type=type, status=status, limit=max(1, int(limit)))

    def web_search(query: str, max_results: int = 5) -> dict[str, Any]:
        """Search the web through the control plane (keyless DuckDuckGo Lite)."""
        from .control.tools import web_search as search

        return search(query, max_results=max(1, int(max_results)))

    def task_state(run_id: str = "") -> dict[str, Any]:
        """Return the current run/task state (latest run when run_id is empty)."""
        run = hub.store.get_run(run_id) if run_id else hub.store.latest_run(
            hub.project.project_id)
        if not run:
            return {"run": None, "tasks": []}
        tasks = [
            {"task_id": t.task_id, "status": t.status, "agent": t.assigned_agent,
             "error": (t.error or "")[:200]}
            for t in hub.store.list_tasks(run["run_id"])
        ]
        return {"run": {k: run.get(k) for k in
                        ("run_id", "status", "main_agent_id", "model",
                         "goal_status", "blocked_reason")},
                "tasks": tasks}

    def bus_events(after_seq: int = 0, limit: int = 50, topic: str = "",
                   kind: str = "") -> dict[str, Any]:
        """Read append-only Context Bus events after a sequence cursor (read-only)."""
        return hub.bus_events(after_seq=int(after_seq), limit=max(1, int(limit)),
                              topic=topic, kind=kind)

    return {
        "memory_bootstrap": memory_bootstrap,
        "memory_search": memory_search,
        "memory_list": memory_list,
        "web_search": web_search,
        "task_state": task_state,
        "bus_events": bus_events,
    }


def build_server(hub: Hub):
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(
        name="agentmesh",
        instructions=(
            "AgentMesh OS control-plane tools. Use memory_bootstrap to load the "
            "project state at startup, memory_search/memory_list to look up prior "
            "knowledge and decisions, web_search for keyless web retrieval, and "
            "task_state to inspect the current run. All tools are read-only."
        ),
    )
    for function in build_tools(hub).values():
        server.tool()(function)
    return server


def run_stdio(project_root: os.PathLike[str] | str) -> None:
    hub = Hub(project_root, auto_discover=False)
    try:
        build_server(hub).run(transport="stdio")
    finally:
        hub.close()


__all__ = ["build_tools", "build_server", "run_stdio"]