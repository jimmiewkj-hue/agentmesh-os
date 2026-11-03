"""Generic executable CLI adapter.

Contract:
  * The control plane writes `prompt.md`, `task.json` and a result template.
  * The adapter passes the prompt via argv/stdin/file according to config and
    exports AGENTLOOP_* environment variables.
  * The agent must write a result JSON object to `result_path`.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from ..models import AgentRecord
from ..util import atomic_write_text, ensure_dir, estimate_tokens, read_text
from .base import BaseAdapter, ExecutionOutcome, ExecutionRequest, ExecutionUsage
from .process import run_process


PLACEHOLDER_FIELDS = ("workdir", "task_dir", "prompt_file", "result_file", "model", "task_id", "project_root")


def _load_json_file(path: str) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


class CliAgentAdapter(BaseAdapter):
    def __init__(self, agent: AgentRecord, config: dict[str, Any] | None = None):
        super().__init__(agent, config)

    # -- command construction --------------------------------------------
    def _resolve_command(self, request: ExecutionRequest, prompt_text: str) -> list[str]:
        raw_command = self.config.get("command") or self.config.get("executable") or ""
        if isinstance(raw_command, str):
            exe = raw_command
            if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
                command: list[str] = ["cmd", "/c", exe]
            else:
                command = [exe]
        elif isinstance(raw_command, list) and raw_command:
            command = [str(v) for v in raw_command]
        else:
            raise ValueError(f"Agent {self.agent.agent_id} 未配置 command/executable")

        args = [str(v) for v in (self.config.get("args") or [])]
        command += args

        prompt_mode = str(self.config.get("prompt_mode") or ("stdin" if not args else "file"))
        if prompt_mode == "arg":
            command.append(prompt_text)
        elif prompt_mode == "stdin":
            pass  # supplied through stdin_text
        elif prompt_mode == "file":
            pass  # prompt path must be present in args
        else:
            raise ValueError(f"未知 prompt_mode: {prompt_mode}")

        values = {
            "workdir": request.workdir,
            "task_dir": request.task_dir,
            "prompt_file": request.prompt_path,
            "result_file": request.result_path,
            "model": request.model,
            "task_id": request.task.task_id,
            "project_root": request.project_root,
        }

        def expand(part: str) -> str:
            out = part
            for key, value in values.items():
                out = out.replace("{" + key + "}", value)
            return out

        command = [expand(c) for c in command]
        return command

    def _stdin_payload(self, prompt_text: str) -> str | None:
        mode = str(self.config.get("prompt_mode") or ("stdin" if not (self.config.get("args") or []) else "file"))
        return prompt_text if mode == "stdin" else None

    def _command_exists(self, command: list[str]) -> bool:
        if not command:
            return False
        exe = command[0]
        if exe.lower() in ("cmd", "cmd.exe"):
            return True
        return shutil.which(exe) is not None or Path(exe).exists()

    # -- execution -------------------------------------------------------
    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        prompt_text = read_text(request.prompt_path, default="")
        try:
            command = self._resolve_command(request, prompt_text)
        except ValueError as exc:
            return ExecutionOutcome(status="failed", error=str(exc))
        if not self._command_exists(command):
            return ExecutionOutcome(status="failed", error=f"Agent 可执行文件不存在: {command[0]}")

        env = {
            "AGENTLOOP_WORKDIR": request.workdir,
            "AGENTLOOP_TASK_DIR": request.task_dir,
            "AGENTLOOP_PROMPT_FILE": request.prompt_path,
            "AGENTLOOP_RESULT_FILE": request.result_path,
            "AGENTLOOP_TASK_ID": request.task.task_id,
            "AGENTLOOP_PROJECT_ROOT": request.project_root,
            "AGENTLOOP_MODEL": request.model,
        }
        env.update({str(k): str(v) for k, v in (self.config.get("env") or {}).items()})
        env.update(request.env)

        timeout = float(self.config.get("timeout_seconds") or request.timeout_seconds)
        proc = run_process(
            command, cwd=request.workdir, timeout=timeout, env=env,
            stdin_text=self._stdin_payload(prompt_text),
            shell=bool(self.config.get("shell", False)),
        )
        log_dir = ensure_dir(Path(request.task_dir) / "logs")
        atomic_write_text(log_dir / "agent.stdout.log", proc.stdout)
        atomic_write_text(log_dir / "agent.stderr.log", proc.stderr)
        logs = (proc.stdout + "\n" + proc.stderr)[-30000:]

        usage, tool_calls, session_id = self._parse_output(proc.stdout, prompt_text, proc.duration_ms)
        payload = _load_json_file(request.result_path)

        if proc.timed_out:
            return ExecutionOutcome(
                status="timeout", usage=usage, tool_calls=tool_calls, result_payload=payload,
                result_path=request.result_path, logs=logs, error=proc.error or "执行超时",
                duration_ms=proc.duration_ms, pid=proc.pid, session_id=session_id,
            )
        if proc.exit_code != 0:
            return ExecutionOutcome(
                status="failed", usage=usage, tool_calls=tool_calls, result_payload=payload,
                result_path=request.result_path, logs=logs,
                error=f"进程退出码 {proc.exit_code}: {proc.stderr.strip()[:500]}",
                duration_ms=proc.duration_ms, pid=proc.pid, session_id=session_id,
            )
        return ExecutionOutcome(
            status="completed",
            text=self._extract_text(proc.stdout),
            usage=usage, tool_calls=tool_calls, result_payload=payload,
            result_path=request.result_path, logs=logs, duration_ms=proc.duration_ms,
            pid=proc.pid, session_id=session_id,
        )

    # -- parsing ---------------------------------------------------------
    def _parse_output(self, stdout: str, prompt_text: str, duration_ms: int) -> tuple[ExecutionUsage, list[dict], str]:
        parse_mode = str(self.config.get("parse") or "text")
        if parse_mode == "jsonl":
            return self.parse_jsonl(stdout, prompt_text, duration_ms)
        usage = ExecutionUsage(
            input_tokens=estimate_tokens(prompt_text),
            output_tokens=estimate_tokens(stdout),
            total_tokens=estimate_tokens(prompt_text) + estimate_tokens(stdout),
            estimated=True,
        ).normalized()
        return usage, [], ""

    @staticmethod
    def _extract_text(stdout: str) -> str:
        return stdout.strip()[-20000:]

    @staticmethod
    def parse_jsonl(stdout: str, prompt_text: str, duration_ms: int) -> tuple[ExecutionUsage, list[dict], str]:
        """Parse a JSON-lines event stream with a permissive, documented convention.

        Recognized shapes:
          {"type":"text","part":{"text":"..."}}
          {"type":"text","text":"..."}
          {"type":"tool","part":{"tool":"Read","state":{...}}}
          {"type":"step_finish","part":{"tokens":{"input":..,"output":..,"cache":{"read":..}},
                                          "cost":..,"toolCalls":..}}
          {"usage":{"inputTokens":..,...}}
        Unknown lines are ignored; raw text is preserved in logs by the caller.
        """
        events: list[dict] = []
        texts: list[str] = []
        usage = ExecutionUsage()
        tool_calls: list[dict] = []
        reported_usage = False
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
            etype = str(event.get("type") or "")
            part = event.get("part") if isinstance(event.get("part"), dict) else {}
            if etype in ("text", "message"):
                text = part.get("text") or event.get("text") or event.get("content") or ""
                if isinstance(text, str) and text:
                    texts.append(text)
            if etype in ("tool", "tool_use", "tool_call", "tool-invoked"):
                name = part.get("tool") or part.get("name") or event.get("tool") or event.get("name") or ""
                tool_calls.append({"name": str(name), "event": etype})
            if etype in ("step_finish", "step-finish", "finish"):
                tokens = part.get("tokens") or event.get("tokens") or {}
                if isinstance(tokens, dict):
                    usage.input_tokens += int(tokens.get("input", 0) or 0)
                    usage.output_tokens += int(tokens.get("output", 0) or 0)
                    usage.reasoning_tokens += int(tokens.get("reasoning", 0) or 0)
                    cache = tokens.get("cache") or {}
                    if isinstance(cache, dict):
                        usage.cached_tokens += int(cache.get("read", 0) or 0)
                    usage.total_tokens += int(tokens.get("total", 0) or 0)
                    reported_usage = True
                cost = part.get("cost", event.get("cost"))
                if isinstance(cost, (int, float)):
                    usage.cost += float(cost)
                tc = part.get("toolCalls") or part.get("tool_calls")
                if isinstance(tc, int):
                    usage.tool_calls += tc
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict) and not reported_usage:
                usage.input_tokens += int(raw_usage.get("inputTokens", raw_usage.get("input_tokens", 0)) or 0)
                usage.output_tokens += int(raw_usage.get("outputTokens", raw_usage.get("output_tokens", 0)) or 0)
                usage.cached_tokens += int(raw_usage.get("cacheReadTokens", raw_usage.get("cached_tokens", 0)) or 0)
                usage.reasoning_tokens += int(raw_usage.get("reasoningTokens", raw_usage.get("reasoning_tokens", 0)) or 0)
                usage.total_tokens += int(raw_usage.get("totalTokens", raw_usage.get("total_tokens", 0)) or 0)
                reported_usage = True
        usage.tool_calls = max(usage.tool_calls, len(tool_calls))
        if not reported_usage:
            usage.input_tokens = estimate_tokens(prompt_text)
            usage.output_tokens = estimate_tokens("\n".join(texts))
            usage.total_tokens = usage.input_tokens + usage.output_tokens
            usage.estimated = True
        usage.normalized()
        session_id = ""
        for event in reversed(events):
            if event.get("sessionID") or event.get("session_id"):
                session_id = str(event.get("sessionID") or event.get("session_id"))
                break
        return usage, tool_calls, session_id

    def health(self) -> dict[str, Any]:
        raw = self.config.get("command") or self.config.get("executable") or ""
        exe = raw[0] if isinstance(raw, list) and raw else str(raw)
        exists = bool(exe) and (shutil.which(str(exe)) is not None or Path(str(exe)).exists())
        return {
            "agent_id": self.agent.agent_id,
            "adapter_type": self.agent.adapter_type,
            "executable": exe,
            "available": exists,
            "status": "online" if exists else "offline",
        }
