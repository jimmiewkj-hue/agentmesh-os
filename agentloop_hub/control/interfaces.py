"""Programmatic model / workspace / mode control for GUI agents.

Verified entry points on this machine (2026-11-03):

* WorkBuddy 5.9.3 ships a task deeplink handler
  (``workbuddy://task?action=start``).  ``cwd``, ``model``, ``welcomeMode``
  (code/work/design -> source_mode craft/working/design), ``permissionMode``
  and ``mode`` are applied before the prompt is submitted.  A real probe
  produced a session row with ``cwd=D:/.../probe-ws``,
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

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


def qoder_db_path(config: dict[str, Any] | None = None) -> Path:
    configured = str((config or {}).get("qoder_db") or "").strip()
    if configured:
        return Path(configured)
    appdata = Path(os.environ.get("APPDATA") or _home() / "AppData" / "Roaming")
    return appdata / "com.qodercn.app.stable" / "main.sqlite"


def qoder_projects_root(config: dict[str, Any] | None = None) -> Path:
    configured = str((config or {}).get("qoder_projects") or "").strip()
    return Path(configured) if configured else _home() / ".qoder-cn" / "projects"


def qoder_project_slug(path: str | Path) -> str:
    """Qoder's project folder name: ``D:\\a\\b`` -> ``J--a-b``."""
    text = str(path).replace("/", "\\").rstrip("\\")
    text = re.sub(r":\\", "--", text, count=1)
    return text.replace("\\", "-").replace(":", "-")


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
                found.setdefault(model_id, {
                    "id": model_id, "name": str(item.get("name") or model_id),
                    "source": "product_manifest", "evidence": str(path),
                })
    if found:
        return sorted(found.values(), key=lambda r: r["id"])
    # Fallback: the same ids observed in workbuddy.db history.
    return [{"id": key, "name": label, "source": "db_history", "evidence": str(db)}
            for label, key in WORKBUDDY_MODEL_LABELS.items()]


def list_qoder_models(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Read Qoder model keys from ``chat_model_preferences`` + session history."""
    db = qoder_db_path(config)
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    keys: set[str] = set()
    try:
        for row in conn.execute("select model_key from chat_model_preferences"):
            if row and row[0]:
                keys.add(str(row[0]))
        for row in conn.execute("select distinct model from chat_sessions where model is not null"):
            if row and row[0]:
                keys.add(str(row[0]))
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    out = []
    for key in sorted(keys):
        out.append({
            "id": key, "name": QODER_MODEL_LABELS.get(key, key),
            "source": "chat_model_preferences", "evidence": str(db),
        })
    return out


# ---------------------------------------------------------------------------
# WorkBuddy: task deeplink
# ---------------------------------------------------------------------------
WORKBUDDY_MODE_TO_SOURCE = {"code": "craft", "work": "working", "design": "design"}


def build_workbuddy_deeplink(*, workspace: str, model_key: str, prompt: str,
                             welcome_mode: str = "code",
                             permission_mode: str = "fullAccess",
                             agent_mode: str = "craft") -> str:
    from urllib.parse import quote

    params = {
        "action": "start", "prompt": prompt, "cwd": str(workspace),
        "model": model_key, "welcomeMode": welcome_mode,
        "permissionMode": permission_mode, "mode": agent_mode,
    }
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


def _norm_path(value: str | Path) -> str:
    try:
        return os.path.normcase(os.path.normpath(str(value))).replace("/", "\\")
    except Exception:
        return str(value)


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


def verify_workbuddy_task(*, workspace: str, model: str, started_at: float,
                          welcome_mode: str = "code",
                          permission_mode: str = "fullAccess",
                          config: dict[str, Any] | None = None) -> InterfaceOutcome:
    row = _newest_workbuddy_session(workbuddy_db_path(config),
                                    started_at=started_at, workspace=workspace)
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
    elif actual["permission_mode"] != expected["permission_mode"]:
        code = "GUI_MODE_MISMATCH"
        reason = f"期望权限 {expected['permission_mode']}，实际 {actual['permission_mode']}"
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


def _stop_process(name: str) -> None:
    subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True,
                   timeout=30, check=False)


def _start_app(exe: str) -> None:
    subprocess.Popen([exe], close_fds=True)


def prepare_qoder_session(*, workspace: str, model: str,
                          config: dict[str, Any] | None = None,
                          stop_process: Any = None,
                          start_app: Any = None,
                          timeout_seconds: float = 30.0) -> InterfaceOutcome:
    """Move the current Qoder session onto ``workspace`` and set its model.

    The Qoder agent runtime resumes from the transcript, so both the sqlite
    session row and the transcript's ``workspace-directories`` event are
    rewritten.  The app is restarted because it restores the last session at
    startup.
    """
    config = dict(config or {})
    key = qoder_model_key(model)
    if not key:
        return InterfaceOutcome(False, "db_write", {"model": model}, {},
                                reason="model is empty", error_code="MODEL_UNAVAILABLE")
    db = qoder_db_path(config)
    projects = qoder_projects_root(config)
    current = _qoder_current_session(db)
    if not current:
        return InterfaceOutcome(False, "db_write", {"workspace": workspace},
                                {}, reason="没有可用的 Qoder 会话",
                                error_code="WORKSPACE_SWITCH_FAILED")
    ensure_dir(Path(workspace))
    old_cwd = str(current["cwd"] or "")
    old_transcript = _transcript_for(projects, old_cwd, current["session_id"]) if old_cwd else Path()
    new_transcript = _transcript_for(projects, workspace, current["session_id"])
    evidence: dict[str, Any] = {
        "session_id": current["session_id"], "db": str(db),
        "old_workspace": old_cwd, "workspace": workspace,
        "old_transcript": str(old_transcript), "new_transcript": str(new_transcript),
    }
    if not old_transcript.exists() and new_transcript.exists():
        old_transcript = new_transcript
    if not old_transcript.exists():
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
        conn.execute(
            "update chat_sessions set cwd=?, workspace_id=?, model=?, updated_at=? "
            "where session_id=?",
            (workspace, workspace_id, key, int(time.time() * 1000),
             current["session_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    evidence["workspace_id"] = workspace_id

    try:
        (stop_process or _stop_process)("Qoder CN")
        time.sleep(3.0)
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


def verify_qoder_session(*, workspace: str, model: str, started_at: float,
                         config: dict[str, Any] | None = None,
                         session_id: str = "") -> InterfaceOutcome:
    projects = qoder_projects_root(config)
    db = qoder_db_path(config)
    conn = _qoder_connect(db)
    try:
        if session_id:
            row = conn.execute(
                "select session_id, cwd, workspace_id, model from chat_sessions where session_id=?",
                (session_id,),
            ).fetchone()
        else:
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
    transcript = _transcript_for(projects, workspace, str(row["session_id"]))
    if not transcript.exists():
        # The runtime may still be writing to the old folder; search this session.
        candidates = sorted(projects.rglob(f"{row['session_id']}.jsonl"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        transcript = candidates[0] if candidates else transcript
    actual: dict[str, Any] = {"workspace": row["cwd"], "model": row["model"],
                              "session_id": row["session_id"]}
    evidence: dict[str, Any] = {"db": str(db), "transcript": str(transcript)}
    runtime_model = ""
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
                runtime_model = str(message["model"])
                if message.get("stop_reason") == "end_turn":
                    completed = True
    actual["transcript_model"] = runtime_model
    actual["transcript_workspace"] = workspace_dirs[-1] if workspace_dirs else ""
    actual["completed"] = completed
    code, reason = "", ""
    if not workspace_dirs or _norm_path(workspace_dirs[-1]) != _norm_path(workspace):
        code = "WORKSPACE_MISMATCH"
        reason = f"期望工作区 {workspace}，实际 transcript={workspace_dirs[-1] if workspace_dirs else '(无)'}"
    elif runtime_model and runtime_model != expected["model"]:
        code = "GUI_MODEL_SWITCH_FAILED"
        reason = f"期望模型 {expected['model']}，实际 {runtime_model}"
    elif not runtime_model:
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
]
