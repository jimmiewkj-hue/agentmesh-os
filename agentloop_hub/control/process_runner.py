"""Shell command execution used by verification (strings, not argv)."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass

from ..adapters.process import kill_tree


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def run_command(command: str, *, cwd: str, timeout: int = 900,
                env: dict[str, str] | None = None) -> CommandResult:
    merged_env = os.environ.copy()
    merged_env.setdefault("PYTHONIOENCODING", "utf-8")
    merged_env.setdefault("GIT_PAGER", "cat")
    merged_env.setdefault("NO_COLOR", "1")
    if env:
        merged_env.update(env)
    start = time.monotonic()
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(
            command, cwd=cwd, env=merged_env, shell=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            creationflags=creationflags,
        )
    except OSError as exc:
        return CommandResult(command, 126, "", str(exc), 0)
    try:
        stdout, stderr = proc.communicate(timeout=max(1, timeout))
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
    duration = int((time.monotonic() - start) * 1000)
    code = proc.returncode if proc.returncode is not None else -9
    return CommandResult(command, code, stdout or "", stderr or "", duration, timed_out)
