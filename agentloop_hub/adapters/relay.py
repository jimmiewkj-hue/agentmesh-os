"""File relay adapter for GUI agents: structured task in, structured result out.

This is the L2 implementation for agents that have no open CLI but can read a
project file and write a result file (Trae, Qoder, ZCode, WorkBuddy, DSH).
The control plane never fabricates a result: a task stays `waiting_user` until a
human (or an external watcher) writes a valid result package.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..models import AgentRecord
from ..util import atomic_write_text, ensure_dir, read_text
from .base import BaseAdapter, ExecutionOutcome, ExecutionRequest
from .cli import _load_json_file

RESULT_TEMPLATE: dict[str, Any] = {
    "task_id": "",
    "agent_id": "",
    "status": "completed",
    "summary": "一句话说明完成情况",
    "findings": [],
    "files_read": [],
    "files_changed": [],
    "commands_run": [],
    "tool_calls": [],
    "tests": [],
    "evidence": [],
    "risks": [],
    "unresolved_items": [],
    "artifacts": [],
    "token_usage": {"input": 0, "output": 0},
    "cost_estimate": {"amount": 0, "currency": "USD", "estimated": True},
    "recommended_next_action": [],
}

RELAY_README = """# AgentMesh OS 接力任务

1. 用你指定的 Agent（{agent}）打开工作目录：
   `{workdir}`
2. 将 `prompt.md` 的全部内容粘贴到该 Agent 的会话中执行。
3. Agent 完成后，把符合 `result-template.json` 结构的 JSON 结果保存为：
   `{result_path}`
   （同时会在 outbox/result.json 留一份副本）
4. 回到 AgentMesh OS 执行 `task submit-result` 或使用 Web 界面导入结果。

注意：
- 只能修改任务范围 `scope` 内的文件。
- 不得把密钥、凭据写入任务文件或结果文件。
- 成果包必须包含 summary / files_changed / evidence / tests / unresolved_items。
"""


class FileRelayAdapter(BaseAdapter):
    def __init__(self, agent: AgentRecord, config: dict[str, Any] | None = None):
        super().__init__(agent, config)

    def relay_dir(self, request: ExecutionRequest) -> Path:
        root = Path(request.project_root) / ".agentloop" / "relay" / request.task.uid.replace(":", "_")
        return root

    def _write_package(self, request: ExecutionRequest) -> Path:
        relay = self.relay_dir(request)
        inbox = ensure_dir(relay / "inbox")
        outbox = ensure_dir(relay / "outbox")
        prompt = read_text(request.prompt_path, default="")
        atomic_write_text(inbox / "prompt.md", prompt)
        task_json = Path(request.task_dir) / "task.json"
        if task_json.exists():
            atomic_write_text(inbox / "task.json", task_json.read_text(encoding="utf-8"))
        template = dict(RESULT_TEMPLATE)
        template["task_id"] = request.task.task_id
        template["agent_id"] = self.agent.agent_id
        atomic_write_text(inbox / "result-template.json",
                          json.dumps(template, ensure_ascii=False, indent=2))
        atomic_write_text(relay / "README.md", RELAY_README.format(
            agent=self.agent.name, workdir=request.workdir,
            result_path=str(outbox / "result.json"),
        ))
        return relay

    def _collect_result(self, request: ExecutionRequest) -> dict[str, Any] | None:
        relay = self.relay_dir(request)
        candidates = [Path(request.result_path), relay / "outbox" / "result.json"]
        for candidate in candidates:
            if candidate.exists():
                return _load_json_file(str(candidate))
        return None

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        relay = self._write_package(request)
        wait_seconds = float(self.config.get("wait_seconds", 0) or 0)
        poll = max(0.2, float(self.config.get("poll_seconds", 1.0) or 1.0))
        deadline = time.monotonic() + wait_seconds if wait_seconds > 0 else 0.0
        payload = self._collect_result(request)
        while payload is None and deadline and time.monotonic() < deadline:
            time.sleep(poll)
            payload = self._collect_result(request)
        if payload is None:
            return ExecutionOutcome(
                status="waiting_user",
                text=f"等待 {self.agent.name} 执行接力任务。任务包已写入 {relay / 'inbox'}",
                result_path=request.result_path,
                extras={"relay_dir": str(relay), "awaiting_result": str(relay / "outbox" / "result.json")},
            )
        return ExecutionOutcome(
            status="completed", result_payload=payload, result_path=request.result_path,
            text="接力成果已回收", extras={"relay_dir": str(relay)},
        )

    def health(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent.agent_id, "adapter_type": "file_relay",
            "available": True, "status": "online",
            "note": "requires user activation in the agent GUI",
        }


class ManualAdapter(FileRelayAdapter):
    """L0/L1: generate a copy-paste handoff, never wait."""

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        relay = self._write_package(request)
        handoff = ensure_dir(Path(request.project_root) / ".agentloop" / "handoffs")
        target = handoff / f"{request.task.uid.replace(':', '_')}-manual.md"
        prompt = read_text(request.prompt_path, default="")
        atomic_write_text(target, (
            f"# 人工接力任务 {request.task.task_id}\n\n"
            f"- 目标 Agent：{self.agent.name}\n"
            f"- 工作目录：{request.workdir}\n"
            f"- 结果保存位置：{relay / 'outbox' / 'result.json'}\n\n"
            "## 任务提示词\n\n"
            f"{prompt}\n"
        ))
        return ExecutionOutcome(
            status="waiting_user", text=f"人工接力包已生成: {target}",
            result_path=request.result_path,
            extras={"relay_dir": str(relay), "handoff": str(target),
                    "awaiting_result": str(relay / "outbox" / "result.json")},
        )
