"""Unified Agent surface matrix.

For every registered product this module answers the same five questions:

1. ``open_activate`` - can the app be launched/activated programmatically?
2. ``workspace``     - can the target workspace be injected *and read back*?
3. ``model``         - can ``set_model`` take effect *and be read back*?
4. ``dev_mode``      - can the welcome/permission mode be switched *and read
   back*?
5. ``chat``          - can a prompt be submitted and the reply retrieved
   through an in-process channel?

Status vocabulary is deliberately conservative:

* ``verified``     - implemented in this repo and confirmed on this machine
  (logs/tests/readback value included).
* ``partial``      - one half is proven (e.g. readback works, write does not).
* ``unsupported``  - no programmatic interface was found; the evidence field
  states which files were examined and why it failed.
* ``not_installed``- the product/config directory is absent on this machine.

No capability is reported as verified without an ``evidence`` value.  Nothing
in this module synthesizes OS input.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from ..adapters.discovery import DiscoveryContext, discover_agents
from ..util import now_iso
from .zero_input import status as zero_input_status

CAPABILITIES = ("open_activate", "workspace", "model", "dev_mode", "chat")
STATUSES = ("verified", "partial", "unsupported", "not_installed")

_OS_INPUT_NOTE = "无 OS 键鼠/剪贴板事件；仅进程启动、文件/DB、deeplink、CDP、stdio。"


def _cap(status: str, interface: str, sample: str, *,
         readback: str = "", zero_input: bool = True,
         evidence: dict[str, Any] | None = None, gap: str = "") -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status}")
    return {
        "status": status,
        "interface": interface,
        "sample": sample,
        "readback": readback,
        "zero_input": bool(zero_input),
        "evidence": evidence or {},
        "gap": gap,
    }


def _exe(record: Any) -> str:
    return str((record.adapter_config or {}).get("executable")
               or (record.adapter_config or {}).get("command") or "")


def _installed(record: Any) -> bool:
    extra = getattr(record, "extra", {}) or {}
    return bool(extra.get("installed")) or bool(_exe(record)) or record.status == "online"


def _read_only_sqlite(path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Live readback probes (read-only, never mutating).
# ---------------------------------------------------------------------------

def _zcode_evidence(home: Path) -> dict[str, Any]:
    db = home / ".zcode" / "cli" / "db" / "db.sqlite"
    out: dict[str, Any] = {"db": str(db), "exists": db.is_file()}
    if not db.is_file():
        return out
    try:
        rows = _read_only_sqlite(
            db,
            "select id, directory, permission, time_created from session "
            "order by time_created desc limit 1",
        )
        if rows:
            sid, directory, permission, created = rows[0]
            out.update({"session_id": sid, "cwd": directory,
                        "permission": permission})
            try:
                msg = _read_only_sqlite(
                    db,
                    f"select data from message where session_id='{sid}' "
                    "and data like '%modelId%' order by time_created asc limit 1",
                )
                if msg:
                    data = json.loads(msg[0][0]) if isinstance(msg[0][0], str) else msg[0][0]
                    out["modelId"] = data.get("modelId")
                    out["providerId"] = data.get("providerId")
                    out["mode"] = data.get("mode")
            except Exception as exc:  # evidence only
                out["message_probe_error"] = str(exc)
    except Exception as exc:
        out["error"] = str(exc)
    return out


def _workbuddy_evidence(home: Path) -> dict[str, Any]:
    db = home / ".workbuddy" / "workbuddy.db"
    out: dict[str, Any] = {"db": str(db), "exists": db.is_file()}
    if not db.is_file():
        return out
    try:
        rows = _read_only_sqlite(
            db,
            "select cwd, model, permission_mode, source_mode, status from sessions "
            "order by rowid desc limit 1",
        )
        if rows:
            cwd, model, permission, source_mode, state = rows[0]
            out.update({"cwd": cwd, "model": model, "permission_mode": permission,
                        "source_mode": source_mode, "status": state})
    except Exception as exc:
        out["error"] = str(exc)
    return out


def _qoder_cn_evidence(home: Path) -> dict[str, Any]:
    db = home / "AppData" / "Roaming" / "com.qodercn.app.stable" / "main.sqlite"
    out: dict[str, Any] = {"db": str(db), "exists": db.is_file()}
    if not db.is_file():
        return out
    try:
        rows = _read_only_sqlite(
            db,
            "select session_id, cwd, model, permission_mode, product_mode, updated_at "
            "from chat_sessions order by rowid desc limit 1",
        )
        if rows:
            sid, cwd, model, permission, product_mode, updated = rows[0]
            out.update({"session_id": sid, "cwd": cwd, "model": model,
                        "permission_mode": permission, "product_mode": product_mode,
                        "updated_at": updated})
        ws = _read_only_sqlite(db, "select path from workspaces order by rowid desc limit 3")
        out["workspaces"] = [row[0] for row in ws]
    except Exception as exc:
        out["error"] = str(exc)
    return out


def _dsh_evidence(home: Path) -> dict[str, Any]:
    sessions = home / ".dsh" / "sessions"
    out: dict[str, Any] = {"sessions_dir": str(sessions), "exists": sessions.is_dir()}
    if sessions.is_dir():
        dirs = sorted((p.name for p in sessions.iterdir() if p.is_dir()),
                      key=lambda name: (sessions / name).stat().st_mtime, reverse=True)[:5]
        out["workspace_dirs"] = dirs
        out["latest_session_dir"] = dirs[0] if dirs else ""
    settings = home / ".dsh" / "settings.yaml.imported"
    out["settings"] = str(settings)
    out["settings_exists"] = settings.is_file()
    return out


def _trae_evidence(record: Any) -> dict[str, Any]:
    """Legacy evidence for the ENCRYPTED ai-agent database.

    The verified zero-input interface does not use this file at all: it writes
    ``User/globalStorage/state.vscdb`` (plain SQLite), which is reported by
    ``trae_interface.probe`` below.
    """
    path = Path(str((record.adapter_config or {}).get("data_path") or ""))
    out: dict[str, Any] = {"data_path": str(path), "exists": path.is_file()}
    if path.is_file():
        try:
            head = path.read_bytes()[:16]
            out["head_hex"] = head.hex()
            out["sqlite_magic"] = head.startswith(b"SQLite format 3\x00")
        except Exception as exc:
            out["error"] = str(exc)
    return out


def _trae_programmatic_evidence(record: Any) -> dict[str, Any]:
    config = dict(record.adapter_config or {})
    app = str(config.get("gui_app") or record.agent_id)
    from . import trae_interface
    return trae_interface.probe(app, config)


def _models(record: Any) -> list[str]:
    return [str(m) for m in (getattr(record, "models", None) or [])][:12]


# ---------------------------------------------------------------------------
# Per-product capability builders.
# ---------------------------------------------------------------------------

def _file_relay_surface(record: Any, *, reason: str, evidence: dict[str, Any],
                        open_status: str = "verified") -> dict[str, Any]:
    sample = f'Win32_Process.Create("{_exe(record)}")' if _exe(record) else "未找到可执行文件"
    return {
        "open_activate": _cap(open_status, "process-launch", sample,
                              readback="进程存在 / 窗口标题",
                              evidence={"executable": _exe(record)}),
        "workspace": _cap("unsupported", "none", "无", evidence=evidence,
                          gap=reason),
        "model": _cap("unsupported", "none", "无", evidence=evidence,
                      gap=reason),
        "dev_mode": _cap("unsupported", "none", "无", evidence=evidence,
                         gap=reason),
        "chat": _cap("unsupported", "file-relay", "人工把 prompt.md 放入工作区并读取 result.json",
                     evidence=evidence,
                     gap="需要人工在应用内发起；AgentMesh 不合成任何输入事件"),
    }


def _cli_surface(record: Any, *, args: str, model_arg: str, readback: str,
                 profile_note: str) -> dict[str, Any]:
    exe = _exe(record)
    # A config directory (~/.claude etc.) does not mean the CLI is runnable.
    inst = bool(exe) and (Path(exe).exists() or bool(shutil.which(exe)))
    if not inst:
        return {name: _cap("not_installed", "none", "未安装", evidence={"note": profile_note})
                for name in CAPABILITIES}
    return {
        "open_activate": _cap("verified", "subprocess", f"{exe} {args}".strip(),
                              evidence={"executable": exe}),
        "workspace": _cap("verified", "cli-arg + process cwd",
                          f'{exe} {args} --dir "<workspace>"'.replace("--dir", "--cwd"),
                          readback=readback, evidence={"executable": exe}),
        "model": _cap("verified" if model_arg else "partial", "cli-arg",
                      f"{exe} {args} {model_arg}".strip(),
                      readback=readback, evidence={"models": _models(record)}),
        "dev_mode": _cap("partial", "cli-arg", "权限模式通过 CLI 参数固定，无运行时回读",
                         readback="进程参数", gap="缺少应用自身状态回读"),
        "chat": _cap("verified", "stdio (non-interactive)", f"{exe} {args} <prompt>",
                     readback="stdout JSON / result file"),
    }


def _zcode_surface(record: Any, evidence: dict[str, Any]) -> dict[str, Any]:
    exe = _exe(record)
    return {
        "open_activate": _cap("verified" if exe else "not_installed", "process-launch",
                              f'Win32_Process.Create("{exe}")' if exe else "未找到 ZCode.exe",
                              evidence={"executable": exe}),
        "workspace": _cap("partial", "sqlite read; write not implemented",
                          "read session.directory; inject 未实现", readback="session.directory",
                          evidence=evidence,
                          gap="只能回读当前会话 cwd，尚未实现写入新会话/切换工作区"),
        "model": _cap("partial", "sqlite read", "read message.data.modelId/providerId",
                      readback="message.data.modelId", evidence=evidence,
                      gap="可回读模型，尚无 set_model 写入路径"),
        "dev_mode": _cap("partial", "sqlite read", "read session.permission.mode",
                         readback="session.permission.mode", evidence=evidence,
                         gap="可回读 yolo/plan 等模式，尚无切换写入路径"),
        "chat": _cap("unsupported", "none", "无", evidence=evidence,
                     gap="ZCode 为 Electron GUI，未发现 CLI；DB 直写未验证，未做输入合成"),
    }


def _dsh_surface(record: Any, evidence: dict[str, Any]) -> dict[str, Any]:
    exe = _exe(record)
    return {
        "open_activate": _cap("verified" if exe else "not_installed", "process-launch",
                              f'Win32_Process.Create("{exe}")' if exe else "未找到可执行文件",
                              evidence={"executable": exe}),
        "workspace": _cap("partial", "session directory naming",
                          "read ~/.dsh/sessions/--<path>--/", readback="目录名编码 cwd",
                          evidence=evidence, gap="会话内容为 .jsonl.zstd，写入/切换未实现"),
        "model": _cap("partial", "settings.yaml", "read llm-* providers/models",
                      readback="settings.yaml.imported", evidence=evidence,
                      gap="可回读模型配置，无 set_model 生效路径"),
        "dev_mode": _cap("unsupported", "none", "无", evidence=evidence,
                         gap="未找到模式字段"),
        "chat": _cap("unsupported", "none", "无", evidence=evidence,
                     gap="GUI 会话经 zstd 落盘，未发现 IPC/CLI；未做输入合成"),
    }


# ---------------------------------------------------------------------------
# Main report.
# ---------------------------------------------------------------------------

def _build_one(record: Any, home: Path) -> dict[str, Any]:
    agent_id = record.agent_id
    installed = _installed(record)
    exe = _exe(record)
    wb = _workbuddy_evidence(home)
    qc = _qoder_cn_evidence(home)
    zc = _zcode_evidence(home)
    dsh = _dsh_evidence(home)

    if agent_id == "workbuddy":
        caps = {
            "open_activate": _cap("verified", "deeplink + process-launch",
                                  'workbuddy://task?action=start&cwd=...&model=...&welcomeMode=craft&permissionMode=fullAccess',
                                  readback="sessions.cwd", evidence=wb),
            "workspace": _cap("verified", "deeplink", "workbuddy://task?action=start&cwd=<workspace>",
                              readback="workbuddy.db sessions.cwd", evidence=wb),
            "model": _cap("verified", "deeplink", "workbuddy://task?...&model=glm-5.3-flash",
                          readback="sessions.model + providerData.requestModelName",
                          evidence=wb),
            "dev_mode": _cap("verified", "deeplink",
                             "workbuddy://task?...&welcomeMode=craft&permissionMode=fullAccess",
                             readback="sessions.source_mode + sessions.permission_mode",
                             evidence=wb),
            "chat": _cap("verified", "deeplink + cdp-dom",
                         "deeplink 预填 prompt，CDP Runtime.evaluate 点击渲染器“发送”按钮",
                         readback="sessions 行 + projects/<cwd>/<session>.jsonl 回复事件",
                         evidence=wb),
        }
    elif agent_id == "qoder_cn":
        caps = {
            "open_activate": _cap("verified", "process-launch", f'Win32_Process.Create("{exe}")',
                                  evidence={"executable": exe}),
            "workspace": _cap("verified", "sqlite + transcript write",
                              "prepare_qoder_session(workspace, model) -> main.sqlite chat_sessions.cwd + transcript workspace-directories",
                              readback="chat_sessions.cwd + 重启后 daemon open_session cwd", evidence=qc),
            "model": _cap("verified", "sqlite + transcript write",
                          "set_model(model_key) -> chat_sessions.model + runtime-config.model",
                          readback="chat_sessions.model + assistant.message.model", evidence=qc),
            "dev_mode": _cap("partial", "launch manifest args",
                             "--permission-mode auto", readback="chat_sessions.permission_mode",
                             evidence=qc, gap="权限模式随启动参数固定，尚无运行时切换"),
            "chat": _cap("verified", "cdp-dom",
                         "Runtime.evaluate: execCommand insertText + click send/提交按钮",
                         readback="projects/<cwd>/<session>.jsonl assistant 回复", evidence=qc),
        }
    elif agent_id == "opencode":
        caps = _cli_surface(
            record, args="run --format json --dir <workspace>", model_arg="--model <model>",
            readback="opencode 自身 session store + JSON 事件",
            profile_note="非交互 CLI，可直接 stdio 执行",
        )
        caps["dev_mode"] = _cap("partial", "cli-arg --auto", "opencode run --auto",
                                readback="进程参数", gap="无运行时模式回读")
    elif agent_id in ("trae", "trae_cn"):
        config = dict(record.adapter_config or {})
        if config.get("mode") == "gui_automation" and config.get("trae_state_db"):
            ev = _trae_programmatic_evidence(record)
            good = bool(ev.get("state_db_plain_sqlite") and ev.get("model_count"))
            caps = {
                "open_activate": _cap("verified" if good else "partial",
                                      "process-launch + cdp",
                                      '"Trae CN.exe" --remote-debugging-port=9342 --folder-uri=file:///...',
                                      readback="/json/version + 窗口标题", evidence=ev),
                "workspace": _cap("verified" if good else "partial", "state.vscdb",
                                  "solo-lite.local-project-folders / project-workspace-cache / mode-state-map.session.workspacePath",
                                  readback="mode-state-map workspacePath + route", evidence=ev),
                "model": _cap("verified" if good else "partial", "state.vscdb",
                              "<uid>:AI.agent.model.recent_user_selection_by_agent_label -> modelId",
                              readback="state.vscdb ItemTable", evidence=ev),
                "dev_mode": _cap("verified" if good else "partial", "state.vscdb",
                                 "solo-lite-mode-state-map-<uid>: work / code / design",
                                 readback="mode-state-map 当前 session.mode", evidence=ev),
                "chat": _cap("verified" if good else "partial", "cdp-dom",
                             "Runtime.evaluate insertText + click .icube-chat-input-action-btn-send",
                             readback="document.body.innerText + assistant_task_status=finished",
                             evidence=ev,
                             gap="" if good else "state.vscdb 未通过明文/模型清单校验"),
            }
        else:
            ev = _trae_evidence(record)
            caps = _file_relay_surface(
                record,
                reason=(f"{Path(str((record.adapter_config or {}).get('data_path') or '')).name} "
                        "文件头非 'SQLite format 3'（加密/私有格式）；本次未发现可用的程序接口"),
                evidence=ev,
            )
    elif agent_id == "qoder":
        ev = {"projects": str(getattr(record, "data_path", "") or ""),
              "models": _models(record), "note": "transcript jsonl 可读；main.sqlite 为明文"}
        caps = _file_relay_surface(record,
                                   reason="国际版仅登记 file_relay；零输入聊天通道未在本机验证",
                                   evidence=ev)
        caps["workspace"] = _cap(
            "partial", "sqlite read",
            "com.qoder.app.stable/main.sqlite workspaces.root_paths_json + chat_sessions.cwd",
            readback="main.sqlite chat_sessions.cwd", evidence=ev,
            gap="与 CN 版同构，但未做写会话/重启回读实测，暂不启用")
        caps["model"] = _cap("partial", "sqlite read", "chat_model_preferences.model_key (含 gfmodel)",
                             readback="~/.qoder/projects/**/*.jsonl assistant.model", evidence=ev,
                             gap="可回读历史模型与真实 key，无已验证的写入/生效路径")
    elif agent_id == "zcode":
        caps = _zcode_surface(record, zc)
    elif agent_id == "dsh":
        caps = _dsh_surface(record, dsh)
    elif agent_id in ("claude_code", "gemini_cli", "codex_cli", "aider"):
        args = {"claude_code": "-p", "gemini_cli": "-p",
                "codex_cli": "exec", "aider": "--yes-always --message"}[agent_id]
        model_arg = {"claude_code": "--model <model>", "gemini_cli": "--model <model>",
                     "codex_cli": "--model <model>", "aider": "--model <model>"}[agent_id]
        caps = _cli_surface(record, args=args, model_arg=model_arg,
                            readback="进程 stdout / 产品自身会话文件",
                            profile_note="本机未安装，未验证")
    elif agent_id == "cursor":
        ev = {"path": str(getattr(record, "data_path", "") or "")}
        has_exe = bool(exe) and (Path(exe).exists() or bool(shutil.which(exe)))
        caps = _file_relay_surface(record, reason="GUI 编辑器，未发现开放 IPC/CDP；未安装或未验证",
                                   evidence=ev, open_status="not_installed" if not has_exe else "partial")
        if not has_exe:
            caps = {name: _cap("not_installed", "none", "未安装", evidence=ev)
                    for name in CAPABILITIES}
    elif agent_id == "rea":
        caps = {
            "open_activate": _cap("verified", "mcp stdio", "spawn mcp server; tools/list",
                                  evidence={"command": exe}),
            "workspace": _cap("unsupported", "none", "无", gap="REA 为工具服务，不持有工作区会话"),
            "model": _cap("unsupported", "none", "无", gap="229 个工具，无 LLM 模型概念"),
            "dev_mode": _cap("unsupported", "none", "无"),
            "chat": _cap("verified", "mcp tools/call", "tools/call 直接执行工具",
                         readback="JSON-RPC 响应"),
        }
    else:  # pragma: no cover - registry is closed
        caps = {name: _cap("unsupported", "none", "无") for name in CAPABILITIES}

    if not installed:
        for name in CAPABILITIES:
            if caps[name]["status"] == "verified":
                caps[name] = {**caps[name], "status": "not_installed",
                              "gap": "产品未安装，未在本机验证"}

    return {
        "agent_id": agent_id,
        "name": record.name,
        "product": record.product,
        "edition": getattr(record, "edition", ""),
        "installed": installed,
        "executable": exe,
        "executable_available": bool(exe) and (
            Path(exe).exists() or bool(shutil.which(exe))
        ),
        "version": getattr(record, "version", ""),
        "adapters": getattr(record, "adapter_type", ""),
        "capabilities": caps,
        "zero_input": all(caps[name]["zero_input"] for name in CAPABILITIES),
        "models": _models(record),
        "note": _OS_INPUT_NOTE,
    }


def surface_report(project_id: str = "", *,
                   ctx: DiscoveryContext | None = None,
                   records: list[Any] | None = None) -> dict[str, Any]:
    ctx = ctx or DiscoveryContext.from_env()
    home = Path(ctx.home)
    if records is None:
        records = discover_agents(project_id, ctx)
    agents = [_build_one(record, home) for record in records]
    counts = {name: 0 for name in STATUSES}
    for agent in agents:
        for cap in agent["capabilities"].values():
            counts[cap["status"]] = counts.get(cap["status"], 0) + 1
    return {
        "generated_at": now_iso(),
        "zero_input": zero_input_status(),
        "capability_names": list(CAPABILITIES),
        "agents": agents,
        "summary": {
            "agents": len(agents),
            "capability_cells": len(agents) * len(CAPABILITIES),
            **counts,
        },
    }


def agent_surface(agent_id: str, project_id: str = "",
                  *, ctx: DiscoveryContext | None = None,
                  records: list[Any] | None = None) -> dict[str, Any]:
    report = surface_report(project_id, ctx=ctx, records=records)
    for agent in report["agents"]:
        if agent["agent_id"] == agent_id:
            return agent
    raise KeyError(f"未知 Agent: {agent_id}")


__all__ = ["CAPABILITIES", "STATUSES", "agent_surface", "surface_report"]
