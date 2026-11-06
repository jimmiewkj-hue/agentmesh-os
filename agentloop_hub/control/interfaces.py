"""Programmatic model / workspace / mode control for GUI agents.

Verified entry points on this machine (2026-11-03):

* WorkBuddy 5.9.3 ships a task deeplink handler
  (``workbuddy://task?action=start``).  ``cwd``, ``model``, ``welcomeMode``
  (code/work/design -> source_mode craft/working/design), ``permissionMode``
  and ``mode`` are applied and the prompt is **prefilled only**; the deeplink
  does not send.  Submission goes through the renderer's own 发送 button via
  CDP (``--remote-debugging-port``, see ``ensure_workbuddy_cdp``).  A real
  2026-11-04 probe produced a session row with
  ``cwd=D:/agentmesh-e2e/zero-input-20261104/project``,
  ``model=deepseek-v4.1-flash``, ``source_mode=craft`` and
  ``permission_mode=fullAccess`` in ``~/.workbuddy/workbuddy.db``.

* Qoder CN 0.5.1 stores workspaces/sessions in
  ``%APPDATA%/com.qodercn.app.stable/main.sqlite``.  ``workspaces`` holds the
  root paths, ``chat_sessions`` holds ``cwd``/``workspace_id``/``model``.  The
  agent runtime resumes from ``~/.qoder-cn/projects/<slug>/<session>.jsonl``;
  its ``workspace-directories`` event is the authoritative workspace and the
  last ``runtime-config`` event carries the model key (``dfmodel`` =
  DeepSeek-Flash, ``gfmodel`` = GLM-5.3-Flash).  A probe moved a transcript to
  ``J--qoder-agent-hub-logs-qoder-probe-ws`` and the next turn wrote
  ``workspace-directories=[D:\\qoder\\agent-hub\\logs\\qoder-probe-ws]`` with
  ``model=dfmodel``.

Everything here is a write to the agent's own documented store or launch
protocol; no mouse coordinates are used.  GUI fallback is kept in the adapter
and is always logged with a reason.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import normalize_capability_level
from ..util import ensure_dir, now_iso, read_json


WORKBUDDY_APP = "workbuddy"
QODER_APP = "qoder_cn"


@dataclass
class InterfaceOutcome:
    ok: bool
    method: str
    expected: dict[str, Any] = field(default_factory=dict)
    actual: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    error_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "method": self.method, "expected": self.expected,
            "actual": self.actual, "evidence": self.evidence,
            "reason": self.reason, "error_code": self.error_code,
        }


# ---------------------------------------------------------------------------
# Model catalogs
# ---------------------------------------------------------------------------
WORKBUDDY_MODEL_LABELS: dict[str, str] = {
    "auto": "Auto",
    "GLM-5.3-Flash": "glm-5.3-flash",
    "GLM-5.3": "glm-5.3",
    "GLM-5.2": "glm-5.2",
    "DeepSeek-V4.1-Flash": "deepseek-v4.1-flash",
    "DeepSeek-V4-Pro": "deepseek-v4-pro",
    "HY4-Preview": "hy4-preview",
    "Minimax-M3.1-Flash": "minimax-m3.1-flash",
    "Kimi-K3.1-Pro": "kimi-k3.1-pro",
    "Mimo-V2.6-Pro": "mimo-v2.6-pro",
    "Step-5-Preview": "step-5-preview",
    "Space-Bunny": "space-bunny",
}

QODER_MODEL_LABELS: dict[str, str] = {
    "dfmodel": "DeepSeek-Flash",
    "gfmodel": "GLM-5.3-Flash",
    "gmodel": "GLM-5.3",
    "kmodel": "Kimi-K3.1-Pro",
    "kmodel_latest": "Kimi-K3.1-Pro",
    "mmodel": "Qwen3.8-Max",
}


def workbuddy_model_key(model: str) -> str:
    text = str(model or "").strip()
    if not text:
        return ""
    lower = text.lower()
    for label, key in WORKBUDDY_MODEL_LABELS.items():
        if label.lower() == lower or key.lower() == lower:
            return key
    return lower


def qoder_model_key(model: str) -> str:
    text = str(model or "").strip()
    if not text:
        return ""
    lower = text.lower()
    for key, label in QODER_MODEL_LABELS.items():
        if label.lower() == lower or key.lower() == lower:
            return key
    if lower in QODER_MODEL_LABELS:
        return lower
    return lower


def _home() -> Path:
    return Path.home()


def workbuddy_db_path(config: dict[str, Any] | None = None) -> Path:
    configured = str((config or {}).get("session_db") or "").strip()
    return Path(configured) if configured else _home() / ".workbuddy" / "workbuddy.db"


def workbuddy_user_id(config: dict[str, Any] | None = None) -> str:
    """The signed-in WorkBuddy uid used as a suffix in localStorage keys."""
    db = workbuddy_db_path(config)
    if not db.exists():
        return ""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "select user_id from sessions where user_id is not null "
                "and user_id != '' order by created_at desc limit 1").fetchone()
            return str(row[0]) if row and row[0] else ""
        finally:
            conn.close()
    except sqlite3.Error:
        return ""


def qoder_db_path(config: dict[str, Any] | None = None) -> Path:
    configured = str((config or {}).get("qoder_db") or "").strip()
    if configured:
        return Path(configured)
    appdata = Path(os.environ.get("APPDATA") or _home() / "AppData" / "Roaming")
    return appdata / "com.qodercn.app.stable" / "main.sqlite"


def qoder_projects_root(config: dict[str, Any] | None = None) -> Path:
    configured = str((config or {}).get("qoder_projects") or "").strip()
    return Path(configured) if configured else _home() / ".qoder-cn" / "projects"


def qoder_process_image(config: dict[str, Any] | None = None) -> str:
    """Qoder CN and Qoder international use different Electron image names."""
    configured = str((config or {}).get("qoder_image") or "").strip()
    return configured or "Qoder CN.exe"


def qoder_sites_root(config: dict[str, Any] | None = None) -> Path:
    configured = str((config or {}).get("qoder_sites") or "").strip()
    return Path(configured) if configured else _home() / ".qoder-cn" / "sites" / "sessions"


def qoder_project_slug(path: str | Path) -> str:
    """Qoder's project folder name: ``D:\\a\\b`` -> ``J--a-b``.

    Qoder replaces every non-alphanumeric separator (``\\``, ``.``, ``_``,
    space) with ``-`` individually, so ``...\\.agentloop\\plan_x`` becomes
    ``...--agentloop-plan-x``.  Matching this exactly is required for the
    desktop to find the migrated transcript.
    """
    text = str(path).replace("/", "\\").rstrip("\\")
    text = re.sub(r":\\", "--", text, count=1)
    return (text.replace("\\", "-").replace(".", "-").replace("_", "-")
            .replace(" ", "-").replace(":", "-"))


def list_workbuddy_models(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Read the real model list from WorkBuddy's cached product manifest."""
    db = workbuddy_db_path(config)
    storage = db.parent / "local_storage"
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(storage.glob("*.info")) if storage.exists() else []:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
            data = json.loads(raw)
        except Exception:
            continue
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            product = (row or {}).get("data") if isinstance(row, dict) else None
            if not isinstance(product, dict):
                continue
            for item in product.get("models") or []:
                if not isinstance(item, dict):
                    continue
                model_id = str(item.get("id") or "").strip()
                if not model_id:
                    continue
                # The manifest also lists editor internals (codewise/completion
                # providers).  Chat models are the ones with a credit price and
                # the chat vendor; anything else is not selectable in the
                # conversation composer.
                vendor = str(item.get("vendor") or "")
                credits = str(item.get("credits") or "").strip()
                if vendor not in ("f", "") or model_id.startswith(("codewise", "completion")):
                    continue
                rate = None
                if credits.lower().startswith("x"):
                    try:
                        rate = float(credits[1:])
                    except ValueError:
                        rate = None
                context = item.get("contextWindow")
                context_default = 0
                context_options: list[int] = []
                if isinstance(context, dict):
                    context_default = int(context.get("defaultLength") or 0)
                    context_options = sorted({
                        int(value) for value in (context.get("supportedLengths") or [])
                        if isinstance(value, (int, float)) and int(value) > 0
                    })
                    if not context_default and context_options:
                        context_default = context_options[0]
                elif isinstance(context, (int, float)):
                    context_default = int(context)
                    context_options = [context_default]
                max_allowed = int(item.get("maxAllowedSize") or 0)
                reasoning = item.get("reasoning") if isinstance(
                    item.get("reasoning"), dict) else {}
                efforts = [str(value) for value in (reasoning.get("supportedEfforts") or [])
                           if str(value or "").strip()]
                found.setdefault(model_id, {
                    "id": model_id, "name": str(item.get("name") or model_id),
                    "rate": rate,
                    "source": "product_manifest", "evidence": str(path),
                    "context_default": context_default,
                    "context_options": context_options,
                    "context_max": max(context_options or [context_default, max_allowed]),
                    "capability_options": efforts,
                    "capability_default": str(reasoning.get("defaultEffort") or ""),
                    "can_disable_thinking": bool(reasoning.get("canDisableThinking")),
                })
    if found:
        return sorted(found.values(), key=lambda r: r["id"])
    # Fallback: the same ids observed in workbuddy.db history.
    return [{"id": key, "name": label, "source": "db_history", "evidence": str(db)}
            for label, key in WORKBUDDY_MODEL_LABELS.items()]


def list_qoder_models(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Read Qoder model keys and their real context/effort preferences."""
    db = qoder_db_path(config)
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    prefs: dict[str, dict[str, Any]] = {}
    try:
        for row in conn.execute(
                "select model_key, context_window, reasoning_effort "
                "from chat_model_preferences"):
            if row and row[0]:
                prefs[str(row[0])] = {
                    "context_window": int(row[1]) if row[1] else 0,
                    "reasoning_effort": str(row[2] or ""),
                }
        for row in conn.execute(
                "select distinct model from chat_sessions where model is not null"):
            if row and row[0]:
                prefs.setdefault(str(row[0]), {"context_window": 0,
                                               "reasoning_effort": ""})
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    observed_max = max((int(item.get("context_window") or 0)
                        for item in prefs.values()), default=0)
    out = []
    for key in sorted(prefs):
        profile = prefs[key]
        context = int(profile.get("context_window") or 0)
        out.append({
            "id": key, "name": QODER_MODEL_LABELS.get(key, key),
            "source": "chat_model_preferences", "evidence": str(db),
            "context_default": context,
            # Qoder persists the selected value but does not publish a
            # supportedLengths list in its local store, so no hard upper
            # bound is invented here.  The largest value observed across the
            # installed model preferences is reported as evidence only.
            "context_options": [],
            "context_max": 0,
            "context_observed_max": observed_max,
            "capability_options": list(QODER_EFFORT_OPTIONS),
            "capability_default": str(profile.get("reasoning_effort") or ""),
            "current_context_length": context,
            "current_capability_level": str(profile.get("reasoning_effort") or ""),
        })
    return out

# ---------------------------------------------------------------------------
# WorkBuddy: task deeplink
# ---------------------------------------------------------------------------
WORKBUDDY_MODE_TO_SOURCE = {"code": "craft", "work": "working", "design": "design"}

QODER_EFFORT_OPTIONS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
QODER_EFFORT_LABELS = {
    "none": "关闭思考", "minimal": "最小", "low": "低", "medium": "中",
    "high": "高", "xhigh": "极高", "max": "最大",
}


def build_workbuddy_deeplink(*, workspace: str, model_key: str, prompt: str,
                             welcome_mode: str = "code",
                             permission_mode: str = "fullAccess",
                             agent_mode: str = "craft") -> str:
    from urllib.parse import quote

    params = {
        "action": "start", "prompt": prompt, "cwd": str(workspace),
        "model": model_key, "welcomeMode": welcome_mode,
        "mode": agent_mode,
    }
    # An empty permission mode means "do not request elevated access"; omit
    # the field instead of sending an invalid value.
    if permission_mode:
        params["permissionMode"] = permission_mode
    query = "&".join(f"{key}={quote(str(value), safe='')}" for key, value in params.items())
    return f"workbuddy://task?{query}"


def _newest_workbuddy_session(db: Path, *, started_at: float,
                              workspace: str) -> dict[str, Any] | None:
    if not db.exists():
        return None
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = list(conn.execute(
            "select id, cwd, status, mode, model, permission_mode, source_mode, created_at "
            "from sessions where created_at >= ? order by created_at desc limit 50",
            (int(started_at * 1000) - 2000,),
        ))
    finally:
        conn.close()
    wanted = _norm_path(workspace)
    for row in rows:
        if _norm_path(row[1]) == wanted:
            return {"session_id": row[0], "cwd": row[1], "status": row[2],
                    "mode": row[3], "model": row[4], "permission_mode": row[5],
                    "source_mode": row[6], "created_at": row[7], "db": str(db)}
    return None


def _workbuddy_session_by_id(db: Path, session_id: str) -> dict[str, Any] | None:
    if not db.exists() or not session_id:
        return None
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "select id, cwd, status, mode, model, permission_mode, source_mode, created_at "
            "from sessions where id=?", (session_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"session_id": row[0], "cwd": row[1], "status": row[2],
            "mode": row[3], "model": row[4], "permission_mode": row[5],
            "source_mode": row[6], "created_at": row[7], "db": str(db)}


def _local_urlopen(url: str, *, timeout: float = 3.0):
    """Loopback probe that ignores HTTP_PROXY (sandbox proxies return 502)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(url, timeout=timeout)


def _norm_path(value: str | Path) -> str:
    try:
        return os.path.normcase(os.path.normpath(str(value))).replace("/", "\\")
    except Exception:
        return str(value)


def ensure_workbuddy_cdp(*, executable: str, port: int = 9223,
                         timeout_seconds: float = 40.0,
                         force_restart: bool = False,
                         allow_restart: bool = False,
                         allow_launch: bool = True) -> InterfaceOutcome:
    """Make sure WorkBuddy's renderer exposes DevTools.

    WorkBuddy's task deeplink only calls ``prepareInput``; it does not send.
    The zero-input path submits by clicking the composer's own send button via
    CDP, which requires the Electron process to be launched with
    ``--remote-debugging-port``.

    Restart policy (root cause of the 2026-11-04 "WorkBuddy 闪退"):
    * CDP already live -> reuse, never kill the app;
    * app not running -> launch it with the flag (no process was lost);
    * app running without CDP -> only restart when the caller explicitly asks
      (``allow_restart``/``force_restart``); otherwise return
      ``APP_RESTART_REQUIRED`` so the WebUI can ask the user and block the
      chain instead of silently killing a working app.
    """
    from .apps import graceful_close, launch_detached, process_running

    def _probe() -> bool:
        try:
            with _local_urlopen(f"http://127.0.0.1:{int(port)}/json/list", timeout=3) as response:
                targets = json.loads(response.read().decode("utf-8"))
            return any(item.get("type") == "page" for item in targets)
        except Exception:
            return False

    if _probe() and not force_restart:
        return InterfaceOutcome(True, "cdp", {"port": int(port), "reused": True},
                                {"source": f"http://127.0.0.1:{port}/json/list"},
                                evidence={"port": int(port), "reused": True})
    if not executable or not Path(executable).exists():
        return InterfaceOutcome(False, "cdp", {"port": int(port)}, {},
                                reason=f"WorkBuddy 可执行文件不存在: {executable}",
                                error_code="GUI_DRIVER_ERROR")
    running = process_running("WorkBuddy.exe")
    if running and not (allow_restart or force_restart):
        return InterfaceOutcome(
            False, "cdp", {"port": int(port), "running": True}, {},
            reason="WorkBuddy 正在运行但未开启程序接口；需要用户确认重启",
            error_code="APP_RESTART_REQUIRED",
            evidence={"port": int(port), "process_running": True,
                      "required": "explicit restart"},
        )
    if not running and not allow_launch:
        return InterfaceOutcome(
            False, "cdp", {"port": int(port), "running": False}, {},
            reason="只读预检不会启动 WorkBuddy",
            error_code="APP_NOT_RUNNING",
            evidence={"port": int(port), "process_running": False,
                      "launch_allowed": False},
        )
    close_info: dict[str, Any] = {"method": "not_running"}
    if running:
        close_info = graceful_close("WorkBuddy.exe")
        time.sleep(1.0)
    pid = launch_detached(
        [executable, f"--remote-debugging-port={int(port)}",
         "--remote-allow-origins=*"],
        env=electron_env(),
    )
    deadline = time.time() + float(timeout_seconds)
    while time.time() < deadline:
        if _probe():
            return InterfaceOutcome(
                True, "cdp", {"port": int(port), "reused": False},
                {"source": f"http://127.0.0.1:{port}/json/list"},
                evidence={"port": int(port), "reused": False,
                          "executable": executable, "pid": pid,
                          "close": close_info},
            )
        time.sleep(1.0)
    return InterfaceOutcome(False, "cdp", {"port": int(port)}, {},
                            reason=f"WorkBuddy CDP 端口 {port} 在 {timeout_seconds}s 内未就绪",
                            error_code="GUI_SEND_FAILED",
                            evidence={"port": int(port), "pid": pid,
                                      "close": close_info})


def prepare_workbuddy_task(*, workspace: str, model: str, prompt: str,
                           welcome_mode: str = "code",
                           permission_mode: str = "fullAccess",
                           agent_mode: str = "craft",
                           launch: Any = None,
                           config: dict[str, Any] | None = None,
                           timeout_seconds: float = 15.0) -> InterfaceOutcome:
    """Launch WorkBuddy's task deeplink and wait for the app to accept it."""
    key = workbuddy_model_key(model)
    if not key:
        return InterfaceOutcome(False, "deeplink", {"model": model}, {},
                                reason="model is empty",
                                error_code="MODEL_UNAVAILABLE")
    known = {row["id"] for row in list_workbuddy_models(config)}
    if known and key not in known:
        return InterfaceOutcome(
            False, "deeplink", {"model": key}, {"available": sorted(known)},
            reason=f"WorkBuddy 模型清单中没有 {key}",
            error_code="MODEL_UNAVAILABLE",
        )
    ensure_dir(Path(workspace))
    uri = build_workbuddy_deeplink(
        workspace=workspace, model_key=key, prompt=prompt,
        welcome_mode=welcome_mode, permission_mode=permission_mode,
        agent_mode=agent_mode,
    )
    started = time.time()
    try:
        if launch:
            launch(uri)
        else:
            os.startfile(uri)  # type: ignore[attr-defined]
    except Exception as exc:
        return InterfaceOutcome(
            False, "deeplink", {"workspace": workspace, "model": key},
            {}, reason=f"深链接启动失败: {exc}",
            error_code="WORKSPACE_SWITCH_FAILED",
        )
    return InterfaceOutcome(
        True, "deeplink",
        {"workspace": workspace, "model": key, "welcome_mode": welcome_mode,
         "permission_mode": permission_mode},
        {},
        evidence={"uri": uri, "started_at": started,
                  "source": "workbuddy task deeplink (action=start)"},
    )


def open_workbuddy_chat(*, session_id: str, launch: Any = None,
                        config: dict[str, Any] | None = None) -> InterfaceOutcome:
    """Bring an existing WorkBuddy conversation to the foreground."""
    from urllib.parse import quote

    session_id = str(session_id or "").strip()
    if not session_id:
        return InterfaceOutcome(False, "deeplink", {}, {},
                                reason="session_id 为空",
                                error_code="MODEL_UNAVAILABLE")
    row = _workbuddy_session_by_id(workbuddy_db_path(config), session_id)
    if not row:
        return InterfaceOutcome(False, "db_read", {"session_id": session_id}, {},
                                reason="WorkBuddy 中不存在该会话",
                                error_code="WORKSPACE_MISMATCH")
    uri = f"workbuddy://chat/{quote(session_id, safe='')}"
    try:
        if launch:
            launch(uri)
        else:
            os.startfile(uri)  # type: ignore[attr-defined]
    except Exception as exc:
        return InterfaceOutcome(False, "deeplink", {"session_id": session_id}, {},
                                reason=f"打开既有会话失败: {exc}",
                                error_code="WORKSPACE_SWITCH_FAILED")
    return InterfaceOutcome(
        True, "deeplink", {"session_id": session_id, "workspace": row["cwd"]},
        {"current_model": row["model"], "current_workspace": row["cwd"]},
        evidence={"uri": uri, "session_id": session_id, "resumed": True},
    )


def _workbuddy_model_row(model: str,
                         config: dict[str, Any] | None = None) -> dict[str, Any]:
    key = str(model or "").strip().lower()
    if not key:
        return {}
    for row in list_workbuddy_models(config):
        for field in ("id", "name", "display_name"):
            if str(row.get(field) or "").strip().lower() == key:
                return row
    return {}


def sync_workbuddy_session_profile(*, workspace: str, started_at: float,
                                    session_id: str = "",
                                    model: str = "",
                                    context_length: int = 0,
                                    capability_level: str = "",
                                    config: dict[str, Any] | None = None,
                                    timeout_seconds: float = 5.0) -> InterfaceOutcome:
    """Write the real context_window/thought_level into WorkBuddy's session store.

    WorkBuddy's own session row carries ``context_window`` (300k/600k/1M) and
    ``thought_level`` (low/high/max).  The deeplink creates the session; we
    update that row before the prompt is submitted so the app's runtime reads
    the user's selected values.
    """
    db = workbuddy_db_path(config)
    session = _workbuddy_session_by_id(db, session_id) if session_id else None
    deadline = time.time() + max(1.0, float(timeout_seconds))
    while not session and time.time() < deadline:
        session = _newest_workbuddy_session(db, started_at=started_at,
                                            workspace=workspace)
        if session:
            break
        time.sleep(0.5)
    if not session:
        # WorkBuddy persists the session row when the first prompt is sent,
        # not when the deeplink is accepted.  The adapter writes again right
        # after submission instead of blocking the task here.
        return InterfaceOutcome(
            True, "db_write", {"workspace": workspace},
            {"deferred": True},
            evidence={"deferred": True, "workspace": workspace,
                      "reason": "WorkBuddy 会话尚未落库，提交后补写"},
        )
    context = int(context_length or 0)
    row = _workbuddy_model_row(model, config)
    labels = {"low": "低", "high": "高", "max": "极高"}
    level = normalize_capability_level(capability_level, labels)
    if not context and not level:
        return InterfaceOutcome(True, "db_write", {}, {"session_id": session["session_id"]},
                                evidence={"source": str(db), "skipped": "no profile"})
    options = [int(value) for value in (row.get("context_options") or [])
               if isinstance(value, (int, float)) and int(value) > 0]
    efforts = [str(value) for value in (row.get("capability_options") or [])
               if str(value or "").strip()]
    if context and options and context not in options:
        return InterfaceOutcome(
            False, "db_write", {"context_length": context}, {"options": options},
            reason=f"WorkBuddy {model or '默认模型'} 支持的上下文长度只有 {options}",
            error_code="INTERFACE_WRITE_FAILED")
    if level and efforts and level not in efforts:
        return InterfaceOutcome(
            False, "db_write", {"thought_level": level}, {"options": efforts},
            reason=f"WorkBuddy {model or '默认模型'} 支持的思考等级只有 {efforts}",
            error_code="INTERFACE_WRITE_FAILED")
    import sqlite3
    try:
        backup = db.with_name(f"{db.name}.agentmesh-{time.strftime('%Y%m%d-%H%M%S')}.bak")
        source = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            target = sqlite3.connect(str(backup))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        conn = sqlite3.connect(str(db), timeout=15)
        try:
            conn.execute("PRAGMA busy_timeout=15000")
            fields = []
            values: list[Any] = []
            if context:
                fields.append("context_window=?")
                values.append(int(context))
            if level:
                fields.append("thought_level=?")
                values.append(level)
            fields.append("updated_at=?")
            values.append(int(time.time() * 1000))
            values.append(session["session_id"])
            conn.execute(f"UPDATE sessions SET {', '.join(fields)} WHERE id=?", values)
            conn.commit()
        finally:
            conn.close()
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "select context_window, thought_level from sessions where id=?",
                (session["session_id"],)).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return InterfaceOutcome(False, "db_write", {}, {},
                                reason=f"写入 WorkBuddy 会话配置失败: {exc}",
                                error_code="INTERFACE_WRITE_FAILED")
    return InterfaceOutcome(
        True, "db_write", {"context_length": context, "capability_level": level},
        {"context_length": int(row[0] or 0), "capability_level": str(row[1] or ""),
         "session_id": session["session_id"]},
        evidence={"source": str(db), "session_id": session["session_id"]},
    )


def verify_workbuddy_task(*, workspace: str, model: str, started_at: float,
                          welcome_mode: str = "code",
                          permission_mode: str = "fullAccess",
                          session_id: str = "",
                          config: dict[str, Any] | None = None) -> InterfaceOutcome:
    db = workbuddy_db_path(config)
    row = _workbuddy_session_by_id(db, session_id) if session_id else \
        _newest_workbuddy_session(db, started_at=started_at, workspace=workspace)
    expected = {"workspace": workspace, "model": workbuddy_model_key(model),
                "source_mode": WORKBUDDY_MODE_TO_SOURCE.get(welcome_mode, "craft"),
                "permission_mode": permission_mode}
    if not row:
        return InterfaceOutcome(False, "db_read", expected, {},
                                reason="workbuddy.db 中未找到该工作区的会话",
                                error_code="WORKSPACE_MISMATCH")
    actual = {"workspace": row["cwd"], "model": row["model"],
              "source_mode": row["source_mode"], "permission_mode": row["permission_mode"],
              "session_id": row["session_id"]}
    evidence = {"source": row["db"], "session_id": row["session_id"],
                "created_at": row["created_at"]}
    code, reason = "", ""
    if _norm_path(actual["workspace"]) != _norm_path(expected["workspace"]):
        code = "WORKSPACE_MISMATCH"
        reason = f"期望工作区 {expected['workspace']}，实际 {actual['workspace']}"
    elif actual["model"] != expected["model"]:
        code = "GUI_MODEL_SWITCH_FAILED"
        reason = f"期望模型 {expected['model']}，实际 {actual['model']}"
    elif actual["source_mode"] != expected["source_mode"]:
        code = "GUI_MODE_MISMATCH"
        reason = f"期望模式 {expected['source_mode']}，实际 {actual['source_mode']}"
    elif expected["permission_mode"] and actual["permission_mode"] != expected["permission_mode"]:
        code = "GUI_MODE_MISMATCH"
        reason = f"期望权限 {expected['permission_mode']}，实际 {actual['permission_mode']}"
    elif not expected["permission_mode"] and str(actual["permission_mode"]).lower() in (
            "fullaccess", "full_access", "bypasspermissions", "bypass_permissions"):
        code = "PERMISSION_TOO_BROAD"
        reason = (
            "未开启完全控制，但 WorkBuddy 会话仍为完全访问；"
            "无法在不使用 OS 输入的情况下收紧，已阻止继续"
        )
    return InterfaceOutcome(not code, "db_read", expected, actual,
                            evidence=evidence, reason=reason, error_code=code)


# ---------------------------------------------------------------------------
# Qoder CN: main.sqlite + transcript
# ---------------------------------------------------------------------------
def _qoder_connect(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise FileNotFoundError(f"Qoder 数据库不存在: {db}")
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def _qoder_current_session(db: Path, workspace: str = "") -> dict[str, Any] | None:
    conn = _qoder_connect(db)
    try:
        row = conn.execute(
            "select session_id, cwd, workspace_id, model, title from chat_sessions "
            "where deleted_at is null and session_kind='standard' "
            "order by updated_at desc limit 1"
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _transcript_for(projects: Path, cwd: str, session_id: str) -> Path:
    return projects / qoder_project_slug(cwd) / f"{session_id}.jsonl"


def _find_qoder_transcript(projects: Path, cwd: str, session_id: str) -> Path:
    """Locate a session transcript even if its workspace folder moved."""
    if cwd:
        path = _transcript_for(projects, cwd, session_id)
        if path.exists():
            return path
    matches = sorted(projects.rglob(f"{session_id}.jsonl"),
                     key=lambda item: item.stat().st_mtime, reverse=True)
    return matches[0] if matches else Path()


def _qoder_session_with_transcript(db: Path, projects: Path) -> tuple[dict[str, Any], Path] | None:
    """Newest chat session that still has a transcript on disk."""
    conn = _qoder_connect(db)
    try:
        rows = [dict(row) for row in conn.execute(
            "select session_id, cwd, workspace_id, model from chat_sessions "
            "where deleted_at is null and session_kind='standard' "
            "order by updated_at desc limit 50"
        )]
    finally:
        conn.close()
    for row in rows:
        path = _find_qoder_transcript(projects, str(row.get("cwd") or ""),
                                     str(row["session_id"]))
        if path.is_file():
            return row, path
    return None, Path()


def _rewrite_qoder_transcript(path: Path, *, workspace: str, model_key: str) -> int:
    """Rewrite workspace events and append the requested runtime model."""
    if not path.exists():
        return 0
    lines: list[str] = []
    changed = 0
    last_runtime_index = -1
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except Exception:
            lines.append(line)
            continue
        if event.get("type") == "workspace-directories":
            event["directories"] = [workspace]
            changed += 1
        if event.get("cwd"):
            event["cwd"] = workspace
        if event.get("type") == "runtime-config":
            last_runtime_index = len(lines)
        lines.append(json.dumps(event, ensure_ascii=False))
    if model_key:
        runtime = {
            "type": "runtime-config", "model": model_key,
            "timestamp": int(time.time() * 1000),
        }
        lines.append(json.dumps(runtime, ensure_ascii=False))
        changed += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def _process_running(image: str) -> bool:
    try:
        proc = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
                              capture_output=True, text=True, timeout=20,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return False
    return image.lower() in (proc.stdout or "").lower()


def _stop_process(name: str) -> None:
    """Kill the Electron image and wait until it is really gone.

    ``taskkill /IM`` needs the executable image name (``Qoder CN.exe``); the
    bare product name matched nothing, so the desktop kept running with its
    in-memory route and ignored every store rewrite.
    """
    image = name if name.lower().endswith(".exe") else f"{name}.exe"
    subprocess.run(["taskkill", "/F", "/IM", image, "/T"], capture_output=True,
                   timeout=30, check=False)
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if not _process_running(image):
            return
        time.sleep(0.5)
    raise RuntimeError(f"{image} 仍在运行，无法重载工作区")


def electron_env() -> dict[str, str]:
    """Environment for launching an Electron app from inside another Electron app.

    WorkBuddy's agent host exports ``ELECTRON_RUN_AS_NODE=1``; a child Electron
    binary that inherits it behaves like plain Node and exits immediately.
    """
    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env.pop("NODE_OPTIONS", None)
    return env


def _start_app(exe: str) -> None:
    args: list[str] = []
    port = str(os.environ.get("AGENTMESH_QODER_CDP_PORT") or "").strip()
    if port:
        args = [f"--remote-debugging-port={port}", "--remote-allow-origins=*"]
    subprocess.Popen([exe, *args], close_fds=True, env=electron_env(),
                     creationflags=0x00000008)


def sync_qoder_model_profile(*, model_key: str, context_length: int = 0,
                              capability_level: str = "",
                              config: dict[str, Any] | None = None) -> InterfaceOutcome:
    """Write Qoder's own model preference row (context_window/effort)."""
    db = qoder_db_path(config)
    key = str(model_key or "").strip()
    level = normalize_capability_level(capability_level, QODER_EFFORT_LABELS)
    if not key:
        return InterfaceOutcome(False, "db_write", {}, {},
                                reason="Qoder 模型 key 为空",
                                error_code="MODEL_UNAVAILABLE")
    if level and level not in QODER_EFFORT_OPTIONS:
        return InterfaceOutcome(
            False, "db_write", {"model_key": key, "capability_level": level},
            {"options": QODER_EFFORT_OPTIONS},
            reason=f"Qoder 不支持思考强度 {level}",
            error_code="INTERFACE_WRITE_FAILED")
    if not context_length and not level:
        return InterfaceOutcome(True, "db_write", {}, {},
                                evidence={"skipped": "no profile"})
    try:
        conn = sqlite3.connect(str(db), timeout=15)
        try:
            conn.execute("PRAGMA busy_timeout=15000")
            row = conn.execute(
                "select 1 from chat_model_preferences where model_key=?", (key,)
            ).fetchone()
            if row:
                fields = []
                values: list[Any] = []
                if context_length:
                    fields.append("context_window=?")
                    values.append(int(context_length))
                if level:
                    fields.append("reasoning_effort=?")
                    values.append(level)
                fields.append("updated_at=?")
                values.append(int(time.time() * 1000))
                values.append(key)
                conn.execute(
                    f"UPDATE chat_model_preferences SET {', '.join(fields)} "
                    "WHERE model_key=?", values)
            else:
                conn.execute(
                    "INSERT INTO chat_model_preferences"
                    "(model_key, visible, context_window, reasoning_effort, updated_at) "
                    "VALUES(?,?,?,?,?)",
                    (key, 1, int(context_length) if context_length else None,
                     level or None, int(time.time() * 1000)))
            conn.commit()
        finally:
            conn.close()
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            current = conn.execute(
                "select context_window, reasoning_effort from chat_model_preferences "
                "where model_key=?", (key,)).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return InterfaceOutcome(False, "db_write", {"model_key": key}, {},
                                reason=f"写入 Qoder 模型偏好失败: {exc}",
                                error_code="INTERFACE_WRITE_FAILED")
    return InterfaceOutcome(
        True, "db_write", {"model_key": key, "context_length": int(context_length),
                           "capability_level": level},
        {"context_length": int(current[0] or 0) if current else 0,
         "capability_level": str(current[1] or "") if current else ""},
        evidence={"source": str(db), "model_key": key},
    )


def prepare_qoder_session(*, workspace: str, model: str,
                          config: dict[str, Any] | None = None,
                          permission_mode: str = "",
                          stop_process: Any = None,
                          start_app: Any = None,
                          timeout_seconds: float = 30.0,
                          allow_restart: bool = False,
                          allow_history_migration: bool = False,
                          owned_session_id: str = "") -> InterfaceOutcome:
    """Move the current Qoder session onto ``workspace`` and set its model.

    The Qoder agent runtime resumes from the transcript, so both the sqlite
    session row and the transcript's ``workspace-directories`` event are
    rewritten.  The app is restarted because it restores the last session at
    startup.

    This operation stops every Qoder process and rewrites the user's current
    transcript.  It is therefore only allowed when the caller explicitly
    authorises a restart *and* history migration (the preflight/restart action
    in the UI).  A normal task preparation gets a structured block instead of
    silently touching the user's own session.
    """
    config = dict(config or {})
    key = qoder_model_key(model)
    if not key:
        return InterfaceOutcome(False, "db_write", {"model": model}, {},
                                reason="model is empty", error_code="MODEL_UNAVAILABLE")
    if not (allow_restart and allow_history_migration):
        return InterfaceOutcome(
            False, "blocked", {"workspace": workspace, "model": key}, {},
            evidence={"owned_session_id": owned_session_id},
            reason=(
                "Qoder 常规执行不允许停止应用或迁移用户历史会话；"
                "请在预检/重启步骤中显式授权，或先为本次运行建立独立会话"
            ),
            error_code="SESSION_BINDING_UNCONFIRMED",
        )
    db = qoder_db_path(config)
    projects = qoder_projects_root(config)
    current = _qoder_current_session(db)
    if not current:
        return InterfaceOutcome(False, "db_write", {"workspace": workspace},
                                {}, reason="没有可用的 Qoder 会话",
                                error_code="WORKSPACE_SWITCH_FAILED")
    ensure_dir(Path(workspace))
    evidence: dict[str, Any] = {"session_id": current["session_id"], "db": str(db),
                                "workspace": workspace}
    try:
        (stop_process or _stop_process)(qoder_process_image(config))
        time.sleep(1.0)
    except Exception as exc:
        return InterfaceOutcome(
            False, "db_write", {"workspace": workspace, "model": key}, {},
            evidence=evidence, reason=f"停止 Qoder 失败: {exc}",
            error_code="WORKSPACE_SWITCH_FAILED",
        )
    current = _qoder_current_session(db) or current
    old_cwd = str(current["cwd"] or "")
    old_transcript = _find_qoder_transcript(projects, old_cwd, current["session_id"])
    new_transcript = _transcript_for(projects, workspace, current["session_id"])
    if not old_transcript.is_file():
        # The GUI may have created an empty chat (no transcript yet).  Migrate
        # the newest session that has real history instead of failing.
        fallback, fallback_path = _qoder_session_with_transcript(db, projects)
        if fallback:
            current = fallback
            old_cwd = str(current["cwd"] or "")
            old_transcript = fallback_path
            new_transcript = _transcript_for(projects, workspace, current["session_id"])
            evidence["fallback_session_id"] = current["session_id"]
    evidence.update({
        "session_id": current["session_id"],
        "old_workspace": old_cwd, "old_transcript": str(old_transcript),
        "new_transcript": str(new_transcript),
    })
    if not old_transcript.is_file():
        return InterfaceOutcome(
            False, "db_write",
            {"workspace": workspace, "model": key}, {},
            evidence=evidence,
            reason="当前 Qoder 会话没有 transcript，无法迁移工作区",
            error_code="WORKSPACE_SWITCH_FAILED",
        )
    ensure_dir(new_transcript.parent)
    if old_transcript != new_transcript:
        shutil.copyfile(old_transcript, new_transcript)
    changed = _rewrite_qoder_transcript(new_transcript, workspace=workspace,
                                        model_key=key)
    evidence["rewritten_events"] = changed

    workspace_id = ""
    conn = _qoder_connect(db)
    try:
        existing = None
        for row in conn.execute("select workspace_id, root_paths_json from workspaces"):
            try:
                roots = json.loads(row["root_paths_json"] or "[]")
            except Exception:
                continue
            if any(_norm_path(item) == _norm_path(workspace) for item in roots):
                existing = row
                break
        if existing:
            workspace_id = str(existing["workspace_id"])
            conn.execute("update workspaces set updated_at=? where workspace_id=?",
                         (int(time.time() * 1000), workspace_id))
        else:
            workspace_id = "ws_" + uuid.uuid4().hex
            stamp = int(time.time() * 1000)
            conn.execute(
                "insert into workspaces (workspace_id, name, workspace_source, "
                "root_paths_json, extra_json, created_at, updated_at, archived, product_mode) "
                "values (?,?,?,?,?,?,?,?,?)",
                (workspace_id, Path(workspace).name, "local",
                 json.dumps([workspace]), "{}", stamp, stamp, 0, "coding"),
            )
        target_row = conn.execute(
            "select execution_target_json from chat_sessions where session_id=?",
            (current["session_id"],),
        ).fetchone()
        target_json = {}
        try:
            target_json = json.loads(target_row["execution_target_json"] or "{}") \
                if target_row else {}
        except Exception:
            target_json = {}
        if isinstance(target_json, dict):
            ws = target_json.get("workspace")
            if not isinstance(ws, dict):
                ws = {}
            ws["id"] = workspace_id
            ws["path"] = workspace
            target_json["workspace"] = ws
        if permission_mode:
            conn.execute(
                "update chat_sessions set cwd=?, workspace_id=?, model=?, "
                "permission_mode=?, execution_target_json=?, updated_at=? "
                "where session_id=?",
                (workspace, workspace_id, key, permission_mode,
                 json.dumps(target_json, ensure_ascii=False),
                 int(time.time() * 1000), current["session_id"]),
            )
            evidence["permission_mode"] = permission_mode
        else:
            conn.execute(
                "update chat_sessions set cwd=?, workspace_id=?, model=?, "
                "execution_target_json=?, updated_at=? where session_id=?",
                (workspace, workspace_id, key, json.dumps(target_json, ensure_ascii=False),
                 int(time.time() * 1000), current["session_id"]),
            )
        conn.commit()
    finally:
        conn.close()
    evidence["workspace_id"] = workspace_id

    # The app restores its last route from Local Storage.  Drop that cache
    # (after a backup) so the next launch reads the new cwd/workspace from
    # main.sqlite instead of the old in-memory route.
    try:
        local = db.parent / "Local Storage" / "leveldb"
        if local.exists():
            backup = db.parent / "Local Storage" / f"leveldb.agentmesh-{int(time.time())}"
            if not backup.exists():
                shutil.copytree(local, backup)
            shutil.rmtree(local, ignore_errors=True)
            evidence["local_storage_reset"] = str(backup)
    except Exception as exc:
        evidence["local_storage_reset_error"] = str(exc)

    # Remove every stale copy of this session transcript so neither the
    # reader nor the runtime can resume an older workspace.
    try:
        removed = []
        for other in projects.rglob(f"{current['session_id']}.jsonl"):
            if other == new_transcript:
                continue
            stale = other.with_suffix(".jsonl.agentmesh-old")
            if not stale.exists():
                shutil.copyfile(other, stale)
            other.unlink()
            removed.append(str(other))
        evidence["removed_stale_transcripts"] = removed
    except Exception as exc:
        evidence["old_transcript_remove_error"] = str(exc)

    # Move the encrypted per-session state folder; it also carries the
    # workspace binding for the resumed session.
    try:
        old_state = projects / qoder_project_slug(old_cwd) / current["session_id"]
        new_state = projects / qoder_project_slug(workspace) / current["session_id"]
        if old_state.exists() and old_state != new_state:
            # Do not copy the encrypted state: it still carries the old
            # workspace binding.  Back it up and let the desktop rebuild the
            # state from main.sqlite + the rewired transcript.
            backup = old_state.parent / f"{current['session_id']}.agentmesh-old"
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            shutil.move(str(old_state), str(backup))
            if new_state.exists():
                stale = new_state.parent / f"{current['session_id']}.agentmesh-old"
                if stale.exists():
                    shutil.rmtree(stale, ignore_errors=True)
                shutil.move(str(new_state), str(stale))
            evidence["session_state"] = "dropped (backup: " + str(backup) + ")"
    except Exception as exc:
        evidence["session_state_error"] = str(exc)

    # The site session file is the third state location the desktop uses to
    # restore its agent panel.  Point it at the new workspace and keep only
    # this session so a stale route cannot win.
    try:
        sites = qoder_sites_root(config)
        ensure_dir(sites)
        kept = []
        for path in sorted(sites.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            location = data.get("location") if isinstance(data, dict) else None
            if not isinstance(location, dict):
                continue
            if str(location.get("sessionId") or "") != current["session_id"]:
                backup = path.with_suffix(".json.agentmesh-old")
                if not backup.exists():
                    shutil.copyfile(path, backup)
                path.unlink()
                continue
            location["projectRoot"] = workspace
            location["workspaceId"] = workspace_id
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            kept.append(str(path))
        if not kept:
            name = hashlib.sha256(current["session_id"].encode()).hexdigest() + ".json"
            path = sites / name
            path.write_text(json.dumps({
                "version": 2, "profile": "desktop", "productId": "qoder-cn",
                "environment": "prod",
                "location": {"sessionId": current["session_id"],
                             "workspaceId": workspace_id, "projectRoot": workspace},
            }, ensure_ascii=False), encoding="utf-8")
            kept.append(str(path))
        evidence["site_sessions"] = kept
    except Exception as exc:
        evidence["site_session_error"] = str(exc)

    try:
        exe = str(config.get("executable") or
                  r"C:\Program Files\Qoder\Qoder CN\Qoder CN.exe")
        (start_app or _start_app)(exe)
    except Exception as exc:
        return InterfaceOutcome(
            False, "db_write", {"workspace": workspace, "model": key}, {},
            evidence=evidence, reason=f"Qoder 重启失败: {exc}",
            error_code="WORKSPACE_SWITCH_FAILED",
        )
    return InterfaceOutcome(
        True, "db_write", {"workspace": workspace, "model": key}, {},
        evidence={**evidence, "source": "main.sqlite + transcript runtime-config"},
    )


def qoder_daemon_cwd(config: dict[str, Any] | None = None,
                     session_id: str = "") -> dict[str, Any]:
    """Read ``sessionId=... session opened cwd=`` from the Qoder SDK daemon log.

    The SDK request is ``open_session({worker_args, cwd})``; the daemon logs
    the effective cwd per daemon session.  The daemon session id equals the
    desktop chat session id that ran the turn, so a per-session lookup is the
    authoritative runtime workspace (utility sessions share the same log and
    usually run in the home directory).
    """
    configured = str((config or {}).get("qoder_daemon_log") or "").strip()
    if configured:
        candidates = [Path(configured)]
    else:
        appdata = Path(os.environ.get("APPDATA") or _home() / "AppData" / "Roaming")
        root = appdata / "com.qodercn.app.stable" / "logs"
        candidates = sorted(root.glob("*/qodercli/qoder-agent-sdk.log"),
                            key=lambda p: p.stat().st_mtime, reverse=True) \
            if root.exists() else []
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        pairs = re.findall(r"sessionId=(\S+) session opened cwd=(.*?) workerArgs=", text)
        if pairs:
            per_session = {sid: cwd.strip() for sid, cwd in pairs}
            wanted = str(session_id or "").strip()
            chosen = per_session.get(wanted, "") if wanted else ""
            return {
                "cwd": chosen or pairs[-1][1].strip(),
                "session_id": wanted if chosen else pairs[-1][0],
                "session_cwd": chosen,
                "last_cwd": pairs[-1][1].strip(),
                "sessions": len(pairs), "log": str(path),
            }
        legacy = re.findall(r"session opened cwd=(.*?) workerArgs=", text)
        if legacy:
            return {"cwd": legacy[-1].strip(), "session_id": "",
                    "session_cwd": "", "last_cwd": legacy[-1].strip(),
                    "sessions": len(legacy), "log": str(path)}
    return {"cwd": "", "session_id": "", "session_cwd": "", "last_cwd": "",
            "sessions": 0, "log": ""}


def verify_qoder_session(*, workspace: str, model: str, started_at: float,
                         config: dict[str, Any] | None = None,
                         session_id: str = "",
                         strict_daemon: bool = True) -> InterfaceOutcome:
    projects = qoder_projects_root(config)
    db = qoder_db_path(config)
    conn = _qoder_connect(db)
    started_ms = int(float(started_at) * 1000) - 2000
    try:
        if session_id:
            # The GUI creates a fresh chat session for the run; the id comes
            # from the transcript the adapter already read the reply from.
            row = conn.execute(
                "select session_id, cwd, workspace_id, model from chat_sessions where session_id=?",
                (session_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "select session_id, cwd, workspace_id, model from chat_sessions "
                "where deleted_at is null and session_kind='standard' and cwd=? "
                "and updated_at >= ? order by updated_at desc limit 1",
                (workspace, started_ms),
            ).fetchone()
            if not row:
                row = conn.execute(
                    "select session_id, cwd, workspace_id, model from chat_sessions "
                    "order by updated_at desc limit 1"
                ).fetchone()
    finally:
        conn.close()
    expected = {"workspace": workspace, "model": qoder_model_key(model)}
    if not row:
        return InterfaceOutcome(False, "transcript_read", expected, {},
                                reason="Qoder 会话不存在", error_code="WORKSPACE_MISMATCH")
    sid = str(row["session_id"])
    transcript = _transcript_for(projects, workspace, sid)
    if not transcript.exists() and row["cwd"]:
        transcript = _transcript_for(projects, str(row["cwd"]), sid)
    if not transcript.exists():
        candidates = sorted(projects.rglob(f"{sid}.jsonl"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        transcript = candidates[0] if candidates else transcript
    actual: dict[str, Any] = {"workspace": row["cwd"], "model": row["model"],
                              "session_id": sid}
    evidence: dict[str, Any] = {"db": str(db), "transcript": str(transcript)}
    runtime_model = ""
    assistant_model = ""
    workspace_dirs: list[str] = []
    completed = False
    if transcript.exists():
        for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event.get("type") == "workspace-directories":
                workspace_dirs = [str(x) for x in event.get("directories") or []]
            if event.get("type") == "runtime-config" and event.get("model"):
                runtime_model = str(event["model"])
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            if message.get("model") and event.get("type") == "assistant":
                # The answering model is authoritative; runtime-config may
                # trail the assistant event and describe the next turn.
                assistant_model = str(message["model"])
                if message.get("stop_reason") == "end_turn":
                    completed = True
    effective_model = assistant_model or runtime_model
    actual["runtime_config_model"] = runtime_model
    actual["transcript_model"] = effective_model
    actual["transcript_workspace"] = workspace_dirs[-1] if workspace_dirs else ""
    actual["completed"] = completed
    daemon = qoder_daemon_cwd(config, session_id=sid)
    actual["daemon_cwd"] = daemon.get("cwd", "")
    actual["daemon_session_cwd"] = daemon.get("session_cwd", "")
    actual["daemon_session_id"] = daemon.get("session_id", "")
    actual["daemon_last_cwd"] = daemon.get("last_cwd", "")
    evidence["daemon_log"] = daemon.get("log", "")
    code, reason = "", ""
    if not strict_daemon:
        # Preflight: the desktop was just restarted and has not opened a turn
        # yet, so the daemon log still shows the previous session's cwd.  The
        # transcript is the authoritative resume state in that window.
        daemon = {}
    if session_id and daemon.get("log") and not daemon.get("session_cwd"):
        code = "WORKSPACE_MISMATCH"
        reason = (f"daemon open_session cwd 未记录会话 {sid}"
                  f"（最后一条 cwd={daemon.get('last_cwd') or '(无)'}）")
    elif daemon.get("cwd") and _norm_path(daemon["cwd"]) != _norm_path(workspace):
        code = "WORKSPACE_MISMATCH"
        reason = f"daemon open_session cwd={daemon['cwd']}，期望 {workspace}"
    elif not workspace_dirs or _norm_path(workspace_dirs[-1]) != _norm_path(workspace):
        code = "WORKSPACE_MISMATCH"
        reason = f"期望工作区 {workspace}，实际 transcript={workspace_dirs[-1] if workspace_dirs else '(无)'}"
    elif effective_model and effective_model != expected["model"]:
        code = "GUI_MODEL_SWITCH_FAILED"
        reason = f"期望模型 {expected['model']}，实际 {effective_model}"
    elif not effective_model:
        code = "GUI_MODEL_SWITCH_FAILED"
        reason = "transcript 中没有 runtime-config 模型"
    return InterfaceOutcome(not code, "transcript_read", expected, actual,
                            evidence=evidence, reason=reason, error_code=code)


def set_model_programmatically(app: str, model: str,
                               config: dict[str, Any] | None = None) -> InterfaceOutcome:
    """Write the agent's own default-model config (no GUI automation)."""
    app = str(app or "").lower()
    if app == WORKBUDDY_APP:
        key = workbuddy_model_key(model)
        known = {row["id"] for row in list_workbuddy_models(config)}
        if key not in known:
            return InterfaceOutcome(False, "config_write", {"model": key},
                                    {"available": sorted(known)},
                                    reason=f"WorkBuddy 模型清单中没有 {key}",
                                    error_code="MODEL_UNAVAILABLE")
        return InterfaceOutcome(True, "deeplink", {"model": key}, {"model": key},
                                evidence={"source": "workbuddy task deeplink"})
    if app == QODER_APP:
        key = qoder_model_key(model)
        known = {row["id"] for row in list_qoder_models(config)}
        if known and key not in known:
            return InterfaceOutcome(False, "config_write", {"model": key},
                                    {"available": sorted(known)},
                                    reason=f"Qoder 模型清单中没有 {key}",
                                    error_code="MODEL_UNAVAILABLE")
        default_file = Path(config.get("qoder_models_default") if config and config.get("qoder_models_default")
                            else _home() / ".qoder-cn" / ".models" / "default")
        try:
            current = read_json(default_file, {}) or {}
            current["key"] = key
            current["updatedAt"] = int(time.time() * 1000)
            ensure_dir(default_file.parent)
            default_file.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            return InterfaceOutcome(False, "config_write", {"model": key}, {},
                                    reason=f"写入 {default_file} 失败: {exc}",
                                    error_code="INTERFACE_WRITE_FAILED")
        return InterfaceOutcome(True, "config_write", {"model": key}, {"model": key},
                                evidence={"file": str(default_file)})
    return InterfaceOutcome(False, "none", {"model": model}, {},
                            reason=f"未找到 {app} 的模型接口",
                            error_code="MODEL_UNAVAILABLE")


__all__ = [
    "InterfaceOutcome", "WORKBUDDY_MODEL_LABELS", "QODER_MODEL_LABELS",
    "WORKBUDDY_MODE_TO_SOURCE", "build_workbuddy_deeplink",
    "prepare_workbuddy_task", "verify_workbuddy_task",
    "prepare_qoder_session", "verify_qoder_session",
    "list_workbuddy_models", "list_qoder_models",
    "workbuddy_model_key", "qoder_model_key", "set_model_programmatically",
    "qoder_project_slug", "qoder_db_path", "workbuddy_db_path",
    "qoder_projects_root", "qoder_process_image", "qoder_sites_root",
    "qoder_daemon_cwd",
]
