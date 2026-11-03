"""MCP (Model Context Protocol) stdio adapter.

用于接入本地 MCP 服务（例如 REA Studio）。适配器执行真实的 JSON-RPC
握手 ``initialize`` → ``notifications/initialized`` → ``tools/list`` /
``tools/call``，不猜测工具参数，也不把未验证的能力写成“可用”。

Agent 任务执行本身不是 MCP 服务的职责：执行任务时仍生成结构化接力包，
由用户或外部调度器驱动 MCP 工具；控制台/API 可以直接列出并调用工具。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from ..errors import AdapterError
from ..models import AgentRecord
from .base import BaseAdapter, ExecutionOutcome, ExecutionRequest
from .relay import FileRelayAdapter

MCP_PROTOCOL_VERSION = "2024-11-05"


class McpSession:
    """Minimal, dependency-free MCP stdio client for one short call sequence."""

    def __init__(self, command: str | list[str], args: list[str] | None = None, *,
                 env: dict[str, str] | None = None, cwd: str = "", timeout: float = 60):
        if isinstance(command, list):
            parts = [str(p) for p in command]
        else:
            parts = [str(command)]
        parts.extend(str(a) for a in (args or []))
        if not parts or not parts[0]:
            raise AdapterError("MCP 命令为空")
        self.parts = parts
        self.env = dict(env or {})
        self.cwd = cwd or None
        self.timeout = max(0.5, float(timeout))
        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=80)
        self._next_id = 1

    # -- lifecycle -----------------------------------------------------
    def start(self) -> "McpSession":
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in self.env.items()})
        try:
            self._proc = subprocess.Popen(
                self.parts, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                bufsize=1, env=env, cwd=self.cwd,
            )
        except FileNotFoundError as exc:
            raise AdapterError(f"未找到 MCP 可执行文件: {self.parts[0]}") from exc
        except OSError as exc:
            raise AdapterError(f"MCP 进程启动失败: {exc}") from exc
        threading.Thread(target=self._read_stdout, daemon=True,
                         name="mcp-stdout").start()
        threading.Thread(target=self._read_stderr, daemon=True,
                         name="mcp-stderr").start()
        return self

    def _read_stdout(self) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            return
        try:
            for line in proc.stdout:
                self._queue.put(line)
        finally:
            self._queue.put(None)

    def _read_stderr(self) -> None:
        proc = self._proc
        if not proc or not proc.stderr:
            return
        try:
            for line in proc.stderr:
                self._stderr.append(line.rstrip())
        except (OSError, ValueError):
            return

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass

    # -- JSON-RPC ------------------------------------------------------
    def _send(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if not proc or not proc.stdin:
            raise AdapterError("MCP 进程未启动")
        try:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise AdapterError(f"MCP 写入失败: {exc}") from exc

    def _request(self, method: str, params: dict[str, Any] | None = None,
                 timeout: float | None = None) -> Any:
        request_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method,
                    "params": params or {}})
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdapterError(
                    f"MCP 请求超时: {method}（{timeout or self.timeout}s）",
                    details={"stderr": "\\n".join(self._stderr)},
                )
            try:
                line = self._queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise AdapterError(
                    f"MCP 服务提前退出: {method}",
                    details={"stderr": "\\n".join(self._stderr)},
                )
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                # Some servers print banners on stdout; ignore non-JSON lines.
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") != request_id:
                # Server-initiated request: answer with method-not-found so the
                # server never blocks waiting for a capability we don't provide.
                if message.get("id") is not None and message.get("method"):
                    self._send({"jsonrpc": "2.0", "id": message["id"],
                                "error": {"code": -32601, "message": "method not supported"}})
                continue
            if "error" in message:
                error = message.get("error") or {}
                raise AdapterError(
                    f"MCP 调用失败: {error.get('message', error)}",
                    details={"method": method, "error": error,
                             "stderr": "\\n".join(self._stderr)},
                )
            return message.get("result")

    def initialize(self) -> dict[str, Any]:
        result = self._request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "agentmesh-os", "version": "1.0.0"},
        })
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result if isinstance(result, dict) else {}

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = ""
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params)
            if not isinstance(result, dict):
                break
            batch = result.get("tools") or []
            tools.extend([t for t in batch if isinstance(t, dict)])
            cursor = str(result.get("nextCursor") or "")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None,
                  timeout: float | None = None) -> dict[str, Any]:
        if not str(name or "").strip():
            raise AdapterError("MCP 工具名不能为空")
        result = self._request("tools/call", {
            "name": str(name), "arguments": arguments or {},
        }, timeout=timeout)
        return result if isinstance(result, dict) else {"content": result}


class McpAdapter(FileRelayAdapter):
    def _config_value(self, *keys: str) -> Any:
        for key in keys:
            value = self.config.get(key)
            if value:
                return value
        return None

    def _session(self) -> McpSession:
        command = self._config_value("command", "executable")
        if not command:
            raise AdapterError("MCP 适配器缺少 command")
        args = self.config.get("args") or []
        env = self.config.get("env") or {}
        return McpSession(
            command, args=list(args) if isinstance(args, list) else [],
            env=dict(env) if isinstance(env, dict) else {},
            cwd=str(self.config.get("cwd") or ""),
            timeout=float(self.config.get("timeout_seconds", 60) or 60),
        )

    def health(self) -> dict[str, Any]:
        started = time.monotonic()
        session = self._session()
        try:
            session.start()
            info = session.initialize()
            tools = session.list_tools()
        except AdapterError as exc:
            return {
                "agent_id": self.agent.agent_id, "adapter_type": "mcp",
                "available": False, "status": "error", "error": exc.message,
                "details": exc.details, "duration_ms": int((time.monotonic() - started) * 1000),
            }
        finally:
            session.close()
        return {
            "agent_id": self.agent.agent_id, "adapter_type": "mcp",
            "available": True, "status": "online",
            "protocol_version": info.get("protocolVersion", ""),
            "server": info.get("serverInfo", {}),
            "tool_count": len(tools),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "note": "tools/list 真实返回",
        }

    def tools(self) -> dict[str, Any]:
        session = self._session()
        try:
            session.start()
            info = session.initialize()
            tools = session.list_tools()
        finally:
            session.close()
        clean = []
        for tool in tools:
            clean.append({
                "name": tool.get("name", ""),
                "description": str(tool.get("description") or "")[:600],
                "input_schema": tool.get("inputSchema") or {},
            })
        return {
            "agent_id": self.agent.agent_id, "count": len(clean),
            "protocol_version": info.get("protocolVersion", ""),
            "server": info.get("serverInfo", {}),
            "tools": sorted(clean, key=lambda t: t["name"]),
        }

    def call(self, name: str, arguments: dict[str, Any] | None = None,
             *, timeout: float | None = None) -> dict[str, Any]:
        session = self._session()
        try:
            session.start()
            session.initialize()
            result = session.call_tool(name, arguments, timeout=timeout)
        finally:
            session.close()
        return {"agent_id": self.agent.agent_id, "tool": name, "result": result}

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        outcome = super().execute(request)
        outcome.extras = dict(outcome.extras or {})
        outcome.extras.update({
            "mcp_server": self.config.get("mcp_server", ""),
            "mcp_config": self.config.get("config_path", ""),
            "note": "MCP 服务通过 tools/call 执行具体工具；任务接力包已生成",
        })
        return outcome


__all__ = ["McpAdapter", "McpSession", "MCP_PROTOCOL_VERSION"]
