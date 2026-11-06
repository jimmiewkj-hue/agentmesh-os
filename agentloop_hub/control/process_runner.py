"""Shell command execution used by verification (strings, not argv)."""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
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
    long_running: bool = False
    # Server readiness is a probe result, never a log-text inference.
    server_ready: bool = False
    probe_url: str = ""
    process_alive_before_stop: bool = False
    cleaned: bool = True
    probe_note: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


_LONG_RUNNING_RE = re.compile(
    r"(?:"
    r"\bnpm\s+run\s+(?:start|dev|serve|preview)\b"
    r"|\bnpm\s+(?:start|run)\b(?=.*\bstart\b)"
    r"|\b(?:yarn|pnpm)\s+(?:start|dev|serve|preview)\b"
    r"|\bvite\b(?!.*\bbuild\b)"
    r"|\bnext\s+dev\b"
    r"|\bnode\s+[^|&;]*\b(?:serve|server)[^|&;]*"
    r"|\bpython(?:3)?\s+-m\s+http\.server\b"
    r"|\bflask\s+run\b|\buvicorn\b|\bstreamlit\s+run\b"
    r")",
    re.IGNORECASE,
)


def is_long_running_command(command: str) -> bool:
    """True for commands that intentionally keep a server process alive."""
    text = str(command or "").strip()
    if not text:
        return False
    if re.search(r"\b(?:curl|wget|playwright|chrome|msedge)\b", text, re.IGNORECASE):
        return False
    return bool(_LONG_RUNNING_RE.search(text))


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.5):
            return True
    except OSError:
        return False


_SERVER_URL_RE = re.compile(
    r"https?://(127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\]):(\d+)", re.IGNORECASE)


def _probe_http(host: str, port: int, timeout: float = 2.0) -> tuple[bool, str]:
    target = "127.0.0.1" if host in ("0.0.0.0", "::", "[::]") else host.strip("[]")
    url = f"http://{target}:{int(port)}/"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return True, f"HTTP {response.status} {url}"
    except urllib.error.HTTPError as exc:
        # Any HTTP response below 500 proves the process is serving.
        return exc.code < 500, f"HTTP {exc.code} {url}"
    except Exception as exc:  # noqa: BLE001 - network errors are diagnostic
        return False, f"{type(exc).__name__}: {exc}"[:300]


def _wait_for_server(proc: subprocess.Popen, timeout: float):
    """Wait for a real listening endpoint, not just a log line."""
    out: list[str] = []
    err: list[str] = []

    def _pump(stream, sink: list[str]) -> None:
        try:
            for line in iter(stream.readline, ""):
                sink.append(line)
        except Exception:
            return

    threads = [
        threading.Thread(target=_pump, args=(proc.stdout, out), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, err), daemon=True),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + max(1.0, float(timeout))
    note = ""
    url = ""
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            note = f"进程在探测前退出（exit {proc.returncode}）"
            break
        matches = _SERVER_URL_RE.findall("".join(out) + "".join(err))
        if matches:
            host, port = matches[-1]
            url = f"http://{host}:{int(port)}/"
            ok, note = _probe_http(host, int(port))
            if ok:
                ready = True
                break
        time.sleep(0.2)
    if not note:
        note = "日志中出现地址但 HTTP 探测未成功" if url else "未探测到监听地址"
    for thread in threads:
        thread.join(timeout=1.0)
    return ready, url, note, out, err


def _start_local_server(cwd: str, host: str, port: int) -> tuple[subprocess.Popen | None, str]:
    """Start the project's own static server for browser verification commands.

    Plans often emit a Chrome screenshot command whose URL assumes a server is
    already running, while the previous smoke_test.py command stopped its own
    server.  Start the project's serve.py (when present), wait for the port,
    and stop it after the command.
    """
    candidates = (
        "tools/serve.py", "serve.py", "tools/server.py", "server.py",
        "scripts/serve.py", "tools/dev_server.py",
    )
    script = next((os.path.join(cwd, name) for name in candidates
                   if os.path.isfile(os.path.join(cwd, name))), "")
    if not script:
        return None, f"未找到本地静态服务脚本（{', '.join(candidates)}）"
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    proc = subprocess.Popen(
        [sys.executable, script, "--port", str(int(port))],
        cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        if _port_open(host, port):
            return proc, ""
        time.sleep(0.25)
    kill_tree(proc.pid)
    return None, f"本地服务 {host}:{port} 20s 内未就绪"


def _absolutize_screenshot(command: str, cwd: str) -> str:
    """Chrome resolves --screenshot relative to its own cwd; pin it."""
    def replace(match: re.Match) -> str:
        path = match.group(2)
        if os.path.isabs(path):
            return match.group(0)
        absolute = os.path.abspath(os.path.join(cwd, path.replace("/", os.sep)))
        return f'--screenshot="{absolute}"'

    return re.sub(r'--screenshot=("?)([^"\s]+)\1', replace, command)


def _chrome_with_isolated_profile(command: str) -> tuple[str, str]:
    """Give headless Chrome/Edge a unique profile to avoid the user's lock."""
    lowered = command.lower()
    if not ("chrome.exe" in lowered or "msedge.exe" in lowered):
        return command, ""
    if "headless" not in lowered or "--user-data-dir" in lowered:
        return command, ""
    profile = tempfile.mkdtemp(prefix="agentmesh-chrome-")
    flags = f'--user-data-dir="{profile}" --no-first-run --disable-extensions'
    if "--headless=new" in command:
        command = command.replace("--headless=new", f"--headless=new {flags}", 1)
    elif "--headless" in command:
        command = command.replace("--headless", f"--headless {flags}", 1)
    return command, profile


def run_command(command: str, *, cwd: str, timeout: int = 900,
                env: dict[str, str] | None = None,
                long_running_timeout: int = 15) -> CommandResult:
    long_running = is_long_running_command(command)
    if long_running:
        # A dev/static server never exits.  Probe it briefly and stop it;
        # waiting the full build timeout is what made `npm run start` hang
        # T003 verification for 15 minutes.
        timeout = min(int(timeout), max(5, int(long_running_timeout)))
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
    # Plans frequently emit PowerShell cmdlets (Get-ChildItem/Test-Path/
    # Select-String).  shell=True on Windows runs cmd.exe, where those fail.
    original_command = command
    server_proc: subprocess.Popen | None = None
    profile_dir = ""
    url = re.search(r"https?://(127\.0\.0\.1|localhost):(\d+)", command)
    if url and not _port_open(url.group(1), int(url.group(2))):
        server_proc, server_error = _start_local_server(
            cwd, url.group(1), int(url.group(2)))
        if server_error:
            return CommandResult(original_command, 125, "", server_error, 0,
                                 long_running=long_running)
    command = _absolutize_screenshot(command, cwd)
    command, profile_dir = _chrome_with_isolated_profile(command)
    powershell = bool(re.search(
        r"\b(?:Test-Path|Get-ChildItem|Get-Content|Set-Content|New-Item|"
        r"Remove-Item|Copy-Item|Move-Item|Select-Object|Select-String|"
        r"Where-Object|ForEach-Object|Get-Item|Resolve-Path)\b",
        command,
    )) or command.lstrip().startswith("&")
    try:
        if powershell and os.name == "nt":
            proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", command],
                cwd=cwd, env=merged_env, shell=False, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                creationflags=creationflags,
            )
        else:
            proc = subprocess.Popen(
                command, cwd=cwd, env=merged_env, shell=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                creationflags=creationflags,
            )
    except OSError as exc:
        if server_proc:
            kill_tree(server_proc.pid)
        if profile_dir:
            shutil.rmtree(profile_dir, ignore_errors=True)
        return CommandResult(original_command, 126, "", str(exc), 0,
                             long_running=long_running)
    try:
        if long_running:
            ready, url, note, out_chunks, err_chunks = _wait_for_server(
                proc, max(1, timeout))
            alive = proc.poll() is None
            if alive:
                kill_tree(proc.pid)
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            stdout = "".join(out_chunks) + (stdout or "")
            stderr = "".join(err_chunks) + (stderr or "")
            duration = int((time.monotonic() - start) * 1000)
            code = proc.returncode if proc.returncode is not None else -9
            return CommandResult(
                original_command, code, stdout or "", stderr or "", duration,
                timed_out=alive, long_running=True, server_ready=ready,
                probe_url=url, process_alive_before_stop=alive, cleaned=alive,
                probe_note=note,
            )
        stdout, stderr = proc.communicate(timeout=max(1, timeout))
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
    finally:
        if server_proc:
            kill_tree(server_proc.pid)
        if profile_dir:
            shutil.rmtree(profile_dir, ignore_errors=True)
    duration = int((time.monotonic() - start) * 1000)
    code = proc.returncode if proc.returncode is not None else -9
    return CommandResult(original_command, code, stdout or "", stderr or "",
                         duration, timed_out, long_running=long_running)
