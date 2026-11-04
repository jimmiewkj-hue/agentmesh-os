"""Zero OS-input policy.

AgentMesh must never synthesize operating-system mouse or keyboard events as
part of its normal execution path.  The only allowed channels are interfaces
that the target application exposes to other programs (CDP / DevTools,
Electron IPC, deeplinks, database and config writes, stdio, CLI arguments).

Policy is evaluated on every call so an environment change takes effect
immediately:

* ``AGENTMESH_DISABLE_GUI_INPUT=1`` - hard lock.  OS input is denied even if
  the diagnostic switch is also set.
* ``AGENTMESH_ALLOW_OS_INPUT=1`` - diagnostic only.  Allows clipboard /
  keyboard / mouse synthesis so a human can compare the legacy path.  It is
  never enabled by AgentMesh itself.
* default - denied.  The zero-input rule is the product default.

Every denial raises :class:`OsInputBlocked`, which the GUI driver converts to
``GuiAutomationError("GUI_INPUT_FORBIDDEN")``.  Callers must surface that code
instead of silently falling back to input synthesis.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OS_INPUT_KINDS = ("clipboard", "keyboard", "mouse")


@dataclass(frozen=True)
class InputPolicy:
    allowed: bool
    mode: str          # "blocked" | "diagnostic"
    reason: str
    disable_locked: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "mode": self.mode,
            "reason": self.reason,
            "disable_locked": self.disable_locked,
            "env": {
                "AGENTMESH_DISABLE_GUI_INPUT": os.environ.get("AGENTMESH_DISABLE_GUI_INPUT", ""),
                "AGENTMESH_ALLOW_OS_INPUT": os.environ.get("AGENTMESH_ALLOW_OS_INPUT", ""),
            },
        }


class OsInputBlocked(RuntimeError):
    """Raised whenever an OS input synthesis call is attempted."""

    code = "GUI_INPUT_FORBIDDEN"

    def __init__(self, kind: str, policy: InputPolicy, detail: str = ""):
        self.kind = kind
        self.policy = policy
        self.detail = detail
        message = (
            f"零 OS 输入策略禁止 {kind} 合成"
            f"（mode={policy.mode}，{policy.reason}）"
        )
        if detail:
            message += f"：{detail}"
        super().__init__(message)

    @property
    def details(self) -> dict[str, Any]:
        return {"kind": self.kind, "policy": self.policy.to_dict(), "detail": self.detail}


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def current_policy() -> InputPolicy:
    disable = _truthy(os.environ.get("AGENTMESH_DISABLE_GUI_INPUT"))
    allow = _truthy(os.environ.get("AGENTMESH_ALLOW_OS_INPUT"))
    if disable:
        return InputPolicy(False, "blocked",
                           "AGENTMESH_DISABLE_GUI_INPUT=1 永久锁死", True)
    if allow:
        return InputPolicy(True, "diagnostic",
                           "AGENTMESH_ALLOW_OS_INPUT=1 显式诊断开关", False)
    return InputPolicy(False, "blocked", "默认零 OS 输入", False)


def os_input_allowed() -> bool:
    return current_policy().allowed


def require_os_input(kind: str, detail: str = "") -> InputPolicy:
    """Allow or reject one OS input synthesis call.

    Returns the effective policy when allowed; raises otherwise.
    """
    if kind not in OS_INPUT_KINDS:
        kind = str(kind or "unknown")
    policy = current_policy()
    if not policy.allowed:
        _audit({"event": "os_input_blocked", "kind": kind, "detail": detail,
                "policy": policy.to_dict()})
        raise OsInputBlocked(kind, policy, detail)
    _audit({"event": "os_input_allowed", "kind": kind, "detail": detail,
            "policy": policy.to_dict()})
    return policy


def _audit_path() -> Path | None:
    configured = str(os.environ.get("AGENTMESH_OS_INPUT_AUDIT") or "").strip()
    return Path(configured) if configured else None


def _audit(payload: dict[str, Any]) -> None:
    path = _audit_path()
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                     **payload}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def status() -> dict[str, Any]:
    """Compact status used by the surface matrix and diagnostics."""
    policy = current_policy()
    return {
        "os_input": "allowed-diagnostic" if policy.allowed else "blocked",
        "mode": policy.mode,
        "reason": policy.reason,
        "disable_locked": policy.disable_locked,
        "allowed_channels": ["cdp", "ipc", "deeplink", "db_config", "stdio", "cli_args"],
    }


__all__ = [
    "InputPolicy", "OsInputBlocked", "current_policy", "os_input_allowed",
    "require_os_input", "status", "OS_INPUT_KINDS",
]
