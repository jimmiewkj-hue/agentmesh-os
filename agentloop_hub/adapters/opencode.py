"""OpenCode CLI adapter (verified against opencode 1.18.32 on this machine).

`opencode run --format json` emits one JSON object per line:
  step_start / text {part.text} / step_finish {part.tokens, part.cost}
The adapter reads those events and requires the agent to write the result file
declared by the control plane, retrying once with `--continue` if it is missing.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from ..models import AgentRecord
from ..util import atomic_write_text, ensure_dir, read_text
from .base import ExecutionOutcome, ExecutionRequest
from .cli import CliAgentAdapter, _load_json_file
from .process import run_process


def resolve_opencode_executable() -> str:
    exe = shutil.which("opencode") or ""
    if not exe:
        return ""
    if os.name == "nt" and exe.lower().endswith(".cmd"):
        # Prefer the real binary shipped by the npm package; it avoids cmd.exe
        # argument quoting entirely.
        node_dir = Path(exe).parent
        candidate = node_dir / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
        if candidate.exists():
            return str(candidate)
    return exe


class OpenCodeAdapter(CliAgentAdapter):
    def __init__(self, agent: AgentRecord, config: dict[str, Any] | None = None):
        super().__init__(agent, config)
        self.executable = str(self.config.get("executable") or resolve_opencode_executable())

    def _base_command(self, request: ExecutionRequest, instruction: str, *,
                      continue_session: bool = False) -> list[str]:
        exe = self.executable
        if not exe:
            raise ValueError("未找到 opencode 可执行文件")
        args: list[str] = [exe, "run", "--format", "json", "--dir", request.workdir]
        model = request.model or str(self.config.get("model") or "")
        if model:
            args += ["--model", model]
        agent_name = str(self.config.get("agent") or "")
        if agent_name:
            args += ["--agent", agent_name]
        # A headless task that needs to write a file otherwise blocks on the
        # interactive permission prompt and produces no output at all.  Auto
        # approval is the default for control-plane runs; callers can still
        # disable it explicitly with ``auto_approve: false``.
        auto = self.config.get("auto_approve", True)
        if str(auto).lower() not in ("0", "false", "no", "off"):
            args.append("--auto")
        if continue_session:
            args.append("--continue")
        extra = [str(a) for a in (self.config.get("extra_args") or [])]
        args += extra
        args.append(instruction)
        if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
            return ["cmd", "/c", *args]
        return args

    @staticmethod
    def _instruction(request: ExecutionRequest) -> str:
        rel_prompt = request.prompt_path
        try:
            rel_prompt = os.path.relpath(request.prompt_path, request.workdir)
            rel_prompt = rel_prompt.replace("\\", "/")
        except ValueError:
            pass
        rel_result = request.result_path
        try:
            rel_result = os.path.relpath(request.result_path, request.workdir)
            rel_result = rel_result.replace("\\", "/")
        except ValueError:
            pass
        return (
            "You are executing one task for AgentMesh OS. "
            f"Read the task package at {rel_prompt} and do exactly what it asks. "
            f"When finished, write the required JSON result to {rel_result}. "
            "Do not modify files outside the declared scope. Do not write the result anywhere else."
        )

    def _repair_instruction(self, request: ExecutionRequest) -> str:
        rel_result = request.result_path
        try:
            rel_result = os.path.relpath(request.result_path, request.workdir).replace("\\", "/")
        except ValueError:
            pass
        return (
            "Your previous run did not produce a valid JSON object at "
            f"{rel_result}. Read {request.prompt_path} again and write the required JSON "
            "result now. Keep all work already done; do not start over and do not modify "
            "files outside the declared scope."
        )

    @staticmethod
    def error_summary(stdout: str, stderr: str) -> str:
        """Extract OpenCode's structured error instead of an empty exit code."""
        for line in reversed((stdout or "").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            error = event.get("error") if event.get("type") == "error" else None
            if not isinstance(error, dict):
                continue
            data = error.get("data") if isinstance(error.get("data"), dict) else {}
            message = (data.get("message") or error.get("message")
                       or data.get("responseBody") or error.get("name") or "")
            if message:
                return str(message)[:800]
        return (stderr or "").strip()[:800]

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        if not self.executable or not (shutil.which(self.executable) or Path(self.executable).exists()):
            return ExecutionOutcome(status="failed", error="opencode 未安装或不可执行")
        instruction = self._instruction(request)
        timeout = float(self.config.get("timeout_seconds") or request.timeout_seconds)
        env = {
            "AGENTLOOP_WORKDIR": request.workdir,
            "AGENTLOOP_TASK_DIR": request.task_dir,
            "AGENTLOOP_PROMPT_FILE": request.prompt_path,
            "AGENTLOOP_RESULT_FILE": request.result_path,
            "AGENTLOOP_TASK_ID": request.task.task_id,
            "AGENTLOOP_MODEL": request.model,
        }
        env.update(request.env)
        log_dir = ensure_dir(Path(request.task_dir) / "logs")

        proc = run_process(
            self._base_command(request, instruction),
            cwd=request.workdir, timeout=timeout, env=env,
        )
        atomic_write_text(log_dir / "opencode.stdout.log", proc.stdout)
        atomic_write_text(log_dir / "opencode.stderr.log", proc.stderr)
        usage, tool_calls, session_id = self.parse_jsonl(proc.stdout, read_text(request.prompt_path), proc.duration_ms)
        payload = _load_json_file(request.result_path)

        error_text = self.error_summary(proc.stdout, proc.stderr)
        if proc.timed_out:
            return ExecutionOutcome(status="timeout", usage=usage, tool_calls=tool_calls,
                                    result_payload=payload, result_path=request.result_path,
                                    logs=(proc.stdout + "\n" + proc.stderr)[-30000:],
                                    error=f"opencode 执行超时{(': ' + error_text) if error_text else ''}",
                                    duration_ms=proc.duration_ms,
                                    pid=proc.pid, session_id=session_id)
        if proc.exit_code != 0:
            return ExecutionOutcome(status="failed", usage=usage, tool_calls=tool_calls,
                                    result_payload=payload, result_path=request.result_path,
                                    logs=(proc.stdout + "\n" + proc.stderr)[-30000:],
                                    error=(f"opencode 退出码 {proc.exit_code}"
                                           f"{(': ' + error_text) if error_text else ''}"),
                                    duration_ms=proc.duration_ms, pid=proc.pid, session_id=session_id)
        if error_text and payload is None:
            return ExecutionOutcome(status="failed", usage=usage, tool_calls=tool_calls,
                                    result_payload=None, result_path=request.result_path,
                                    logs=(proc.stdout + "\n" + proc.stderr)[-30000:],
                                    error=f"opencode 返回错误: {error_text}",
                                    duration_ms=proc.duration_ms, pid=proc.pid, session_id=session_id)

        repair_logs = ""
        if payload is None and int(self.config.get("repair_attempts", 1)) > 0:
            repair = run_process(
                self._base_command(request, self._repair_instruction(request), continue_session=True),
                cwd=request.workdir, timeout=min(timeout, 600), env=env,
            )
            repair_logs = repair.stdout + "\n" + repair.stderr
            atomic_write_text(log_dir / "opencode.repair.stdout.log", repair.stdout)
            atomic_write_text(log_dir / "opencode.repair.stderr.log", repair.stderr)
            u2, t2, s2 = self.parse_jsonl(repair.stdout, read_text(request.prompt_path), repair.duration_ms)
            usage.input_tokens += u2.input_tokens
            usage.output_tokens += u2.output_tokens
            usage.cached_tokens += u2.cached_tokens
            usage.reasoning_tokens += u2.reasoning_tokens
            usage.total_tokens += u2.total_tokens
            usage.cost += u2.cost
            usage.tool_calls += u2.tool_calls
            tool_calls.extend(t2)
            session_id = s2 or session_id
            usage.normalized()
            payload = _load_json_file(request.result_path)

        return ExecutionOutcome(
            status="completed", text=self._extract_text(proc.stdout),
            usage=usage, tool_calls=tool_calls, result_payload=payload,
            result_path=request.result_path,
            logs=((proc.stdout + "\n" + proc.stderr + "\n" + repair_logs)[-30000:]),
            duration_ms=proc.duration_ms, pid=proc.pid, session_id=session_id,
            extras={"result_written": payload is not None},
        )

    def health(self) -> dict[str, Any]:
        available = bool(self.executable) and (
            shutil.which(self.executable) is not None or Path(self.executable).exists()
        )
        return {
            "agent_id": self.agent.agent_id, "adapter_type": "opencode",
            "executable": self.executable, "available": available,
            "status": "online" if available else "offline",
        }
