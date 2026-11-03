"""Subprocess execution with timeout and Windows process-tree termination."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class ProcessResult:
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    pid: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command, "exit_code": self.exit_code,
            "stdout": self.stdout, "stderr": self.stderr,
            "duration_ms": self.duration_ms, "timed_out": self.timed_out,
            "pid": self.pid, "error": self.error,
        }


def kill_tree(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=30)
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(pid), 15)
        except Exception:
            try:
                os.kill(pid, 15)
            except Exception:
                pass


def run_process(command: list[str], *, cwd: str | os.PathLike[str],
                timeout: float, env: dict[str, str] | None = None,
                stdin_text: str | None = None, shell: bool = False,
                encoding: str = "utf-8") -> ProcessResult:
    merged_env = os.environ.copy()
    merged_env.setdefault("PYTHONIOENCODING", "utf-8")
    merged_env.setdefault("NO_COLOR", "1")
    if env:
        merged_env.update({str(k): str(v) for k, v in env.items()})
    start = time.monotonic()
    creationflags = 0
    preexec_fn = None
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        preexec_fn = os.setsid
    try:
        proc = subprocess.Popen(
            command, cwd=str(cwd), env=merged_env, shell=shell,
            stdin=subprocess.PIPE if stdin_text is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding=encoding, errors="replace", creationflags=creationflags,
            preexec_fn=preexec_fn,
        )
    except FileNotFoundError as exc:
        return ProcessResult(command, 127, "", str(exc), int((time.monotonic() - start) * 1000),
                             error=f"命令不存在: {command[0] if command else ''}")
    except OSError as exc:
        return ProcessResult(command, 126, "", str(exc), int((time.monotonic() - start) * 1000),
                             error=str(exc))
    pid = proc.pid
    timed_out = False
    try:
        stdout, stderr = proc.communicate(input=stdin_text, timeout=max(1.0, float(timeout)))
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(pid)
        try:
            stdout, stderr = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
    duration = int((time.monotonic() - start) * 1000)
    code = proc.returncode if proc.returncode is not None else -9
    return ProcessResult(
        command=list(command), exit_code=code, stdout=stdout or "", stderr=stderr or "",
        duration_ms=duration, timed_out=timed_out, pid=pid,
        error="执行超时" if timed_out else "",
    )
