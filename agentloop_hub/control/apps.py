"""Desktop application process helpers.

Restarting a user's running editor is a destructive-looking action even when it
is required to enable its DevTools endpoint.  Every caller here is an explicit
user action (WebUI button or preflight).  Closing is graceful first; a forced
kill is only the last resort and is reported in the evidence log.
"""

from __future__ import annotations

import subprocess
import time
from typing import Any

DETACHED_PROCESS = 0x00000008


def process_running(image: str) -> bool:
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return image.lower() in (proc.stdout or "").lower()


def graceful_close(image: str, *, timeout_seconds: float = 12.0) -> dict[str, Any]:
    """Close an app by WM_CLOSE, forcing only after the grace period."""
    started = time.monotonic()
    if not process_running(image):
        return {"ok": True, "method": "not_running", "image": image,
                "elapsed_ms": 0}
    graceful = subprocess.run(
        ["taskkill", "/IM", image],
        capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace",
    )
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    while time.monotonic() < deadline:
        if not process_running(image):
            return {
                "ok": True, "method": "graceful", "image": image,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "graceful_stdout": (graceful.stdout or "").strip()[:300],
            }
        time.sleep(0.5)
    forced = subprocess.run(
        ["taskkill", "/F", "/IM", image, "/T"],
        capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace",
    )
    time.sleep(1.0)
    return {
        "ok": not process_running(image), "method": "forced", "image": image,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "graceful_stdout": (graceful.stdout or "").strip()[:300],
        "forced_stdout": (forced.stdout or "").strip()[:300],
    }


def launch_detached(args: list[str], *, env: dict[str, str] | None = None) -> int:
    proc = subprocess.Popen(
        args, close_fds=True, env=env,
        creationflags=DETACHED_PROCESS,
    )
    return int(getattr(proc, "pid", 0) or 0)


__all__ = ["DETACHED_PROCESS", "graceful_close", "launch_detached", "process_running"]
