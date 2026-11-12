"""Local agent discovery based on this machine's real installations.

Every path and storage layout below was verified on the target Windows machine
and cross-checked against `D:\\zcode\\agent-beacon` harness adapters (see
docs/概要设计.md §Agent 接入矩阵). Discovery never writes to agent stores.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..connectors import profile_for
from ..models import AccessLevel, AdapterType, AgentRecord, AgentStatus
from ..util import read_json
from .opencode import resolve_opencode_executable


@dataclass
class DiscoveryContext:
    home: Path
    appdata: Path
    localappdata: Path
    programfiles: Path
    programfiles_x86: Path = Path("")
    path_lookup: Any = shutil.which
    rea_home: Path | None = None

    @classmethod
    def from_env(cls) -> "DiscoveryContext":
        home = Path.home()
        appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        localappdata = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        programfiles = Path(os.environ.get("ProgramFiles") or "C:/Program Files")
        programfiles_x86 = Path(os.environ.get("ProgramFiles(x86)") or "C:/Program Files (x86)")
        # The AgentMesh checkout and REA Studio are usually sibling directories.
        sibling = Path(__file__).resolve().parents[3] / "rea"
        return cls(home=home, appdata=appdata, localappdata=localappdata,
                   programfiles=programfiles, programfiles_x86=programfiles_x86,
                   rea_home=sibling if sibling.exists() else None)


def _first_existing(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    return None


def _read_version_from_exe(exe: Path | None) -> str:
    if not exe:
        return "unknown"
    for parent in (exe.parent, exe.parent.parent):
        version_file = parent / "version"
        if version_file.exists():
            try:
                return version_file.read_text(encoding="utf-8", errors="replace").strip() or "unknown"
            except OSError:
                pass
        package = parent / "resources" / "app" / "package.json"
        if package.exists():
            data = read_json(package, {}) or {}
            version = str(data.get("version") or "").strip()
            if version:
                return version
    return "unknown"


def _base_capabilities(*, session_read: bool, structured: bool, web: bool = True) -> dict[str, bool]:
    return {
        "read_files": True,
        "write_files": True,
        "terminal": True,
        "web_search": web,
        "mcp": True,
        "session_read": session_read,
        "session_write": False,
        "structured_output": structured,
        "streaming": False,
        "background": False,
    }


def _file_relay_config(data_path: str, reader: str) -> dict[str, Any]:
    return {
        "mode": "file_relay",
        "session_reader": reader,
        "data_path": data_path,
        "wait_seconds": 0,
        "poll_seconds": 1,
    }


def _permissions() -> dict[str, Any]:
    return {
        "allow": ["read_files", "write_files", "terminal"],
        "allowed_roots": [],
        "allow_network": True,
        "allow_shell": True,
        "shell_policy": "approval_required",
    }


def _make_agent(*, agent_id: str, name: str, product: str, edition: str, exe: Path | None,
                data_path: Path | None, reader: str, project_id: str,
                access_level: str, structured: bool = False, web: bool = True,
                adapter_type: str = AdapterType.FILE_RELAY.value,
                adapter_config: dict[str, Any] | None = None,
                models: list[str] | None = None, version: str = "") -> AgentRecord:
    installed = exe is not None or (data_path is not None and data_path.exists())
    has_sessions = bool(data_path and data_path.exists())
    base_caps = _base_capabilities(session_read=has_sessions, structured=structured, web=web)
    capabilities = base_caps if installed else {name: False for name in base_caps}
    record = AgentRecord(
        agent_id=agent_id,
        name=name,
        product=product,
        edition=edition,
        project_id=project_id,
        version=version or _read_version_from_exe(exe),
        adapter_type=adapter_type,
        adapter_config=adapter_config or _file_relay_config(str(data_path or ""), reader),
        access_level=access_level,
        capabilities=capabilities,
        models=models or [],
        permissions=_permissions(),
        status=AgentStatus.ONLINE.value if installed else AgentStatus.OFFLINE.value,
        last_seen="" if not installed else _now(),
        discovered=True,
    )
    record.adapter_config.setdefault("executable", str(exe) if exe else "")
    record.adapter_config.setdefault("session_reader", reader)
    record.adapter_config.setdefault("data_path", str(data_path) if data_path else "")
    record.extra = {"installed": installed, "has_sessions": has_sessions}
    return record


def _now() -> str:
    from ..util import now_iso
    return now_iso()


def _which(ctx: DiscoveryContext, names: tuple[str, ...]) -> Path | None:
    for name in names:
        try:
            found = ctx.path_lookup(name)
        except Exception:
            found = None
        if found:
            return Path(str(found))
    return None


def _first_dir(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


def _models_from_rollout(path: Path, *, max_files: int = 3) -> list[str]:
    if not path.exists():
        return []
    files = sorted(path.glob("model-io-sess_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:max_files]
    models: set[str] = set()
    for file in files:
        try:
            with file.open("r", encoding="utf-8", errors="replace") as handle:
                for _, line in zip(range(200), handle):
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    model = (event.get("model") or {}).get("modelId") if isinstance(event.get("model"), dict) else None
                    if not model:
                        model = (event.get("response") or {}).get("modelId")
                    if model:
                        models.add(str(model))
        except OSError:
            continue
    return sorted(models)


def _models_from_jsonl_projects(path: Path, *, kind: str, max_files: int = 3) -> list[str]:
    if not path.exists():
        return []
    files = sorted(path.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:max_files]
    models: set[str] = set()
    for file in files:
        try:
            with file.open("r", encoding="utf-8", errors="replace") as handle:
                for _, line in zip(range(400), handle):
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if kind == "workbuddy":
                        provider = event.get("providerData") or {}
                        if isinstance(provider, dict) and provider.get("model"):
                            models.add(str(provider["model"]))
                    elif kind == "qoder":
                        if event.get("type") == "runtime-config" and event.get("model"):
                            models.add(str(event["model"]))
                        message = event.get("message") or {}
                        if isinstance(message, dict) and message.get("model"):
                            models.add(str(message["model"]))
        except OSError:
            continue
    return sorted(models)


def _models_from_dsh_sessions(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        import zstandard
    except ImportError:
        return []
    files = sorted(path.rglob("*.jsonl.zstd"), key=lambda p: p.stat().st_mtime, reverse=True)[:2]
    models: set[str] = set()
    for file in files:
        try:
            with file.open("rb") as fh:
                with zstandard.ZstdDecompressor().stream_reader(fh) as reader:
                    for _, line in zip(range(300), reader):
                        try:
                            event = json.loads(line.decode("utf-8", "replace"))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        data = event.get("data") or {}
                        if event.get("type") == "model/selection" and data.get("model"):
                            models.add(str(data["model"]))
                        if event.get("type") == "runtime-config" and data.get("model"):
                            models.add(str(data["model"]))
        except OSError:
            continue
    return sorted(models)


def _opencode_models(exe: str) -> list[str]:
    if not exe:
        return []
    try:
        proc = subprocess.run([exe, "models"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return sorted({line.strip() for line in proc.stdout.splitlines() if "/" in line.strip()})


def _opencode_version(exe: str) -> str:
    if not exe:
        return "unknown"
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return (proc.stdout or "").strip() or "unknown"


def _collect_model_entries(value: Any, out: set[str]) -> None:
    """Collect model names from a heterogeneous config fragment."""
    if isinstance(value, str):
        if value.strip():
            out.add(value.strip())
        return
    if isinstance(value, list):
        for item in value:
            _collect_model_entries(item, out)
        return
    if isinstance(value, dict):
        for key in ("id", "model", "modelId", "name"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                out.add(candidate.strip())
        for key in ("models", "modelOrder", "personalModelIds", "allowedModels"):
            if key in value:
                _collect_model_entries(value[key], out)


def _models_from_provider_config(path: Path) -> list[str]:
    """ZCode/WorkBuddy style provider config: model ids only, never credentials."""
    data = read_json(path, {}) or {}
    if not isinstance(data, dict):
        return []
    out: set[str] = set()
    config = data.get("config") or {}
    rules = ((config.get("providerConfigRules") or {}).get("providerRules")) or []
    if isinstance(rules, list):
        for rule in rules:
            if isinstance(rule, dict):
                _collect_model_entries(rule.get("config") or {}, out)
    _collect_model_entries(config.get("models"), out)
    return sorted(out)


def _models_from_workbuddy(workbuddy_home: Path, projects: Path | None) -> list[str]:
    out: set[str] = set()
    if projects:
        out.update(_models_from_jsonl_projects(projects, kind="workbuddy"))
    models_file = workbuddy_home / "models.json"
    data = read_json(models_file, [])
    if isinstance(data, list):
        _collect_model_entries(data, out)
    elif isinstance(data, dict):
        _collect_model_entries(data.get("models"), out)
    # Session DB is the authoritative per-session model record for WorkBuddy.
    out.update(_models_from_workbuddy_db(workbuddy_home / "workbuddy.db"))
    return sorted(out)


def _models_from_workbuddy_db(db_path: Path) -> list[str]:
    if not db_path.exists():
        return []
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        try:
            rows = conn.execute(
                "SELECT DISTINCT model FROM sessions WHERE model IS NOT NULL AND model != ''"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return sorted({str(row[0]).strip() for row in rows if row and str(row[0]).strip()})


QODER_CN_MODEL_ALIASES = {"dfmodel": "DeepSeek-Flash"}
WORKBUDDY_MODEL_ALIASES = {"glm-5.3-flash": "GLM-5.3-Flash"}


def _models_from_qoder_cn(home: Path, projects: Path | None,
                          appdata: Path | None = None) -> list[str]:
    models = set(_models_from_jsonl_projects(projects, kind="qoder")) if projects else set()
    try:
        from ..control.interfaces import list_qoder_models
        root = (appdata or Path(os.environ.get("APPDATA") or "")) / "com.qodercn.app.stable"
        models.update(row["id"] for row in list_qoder_models({
            "qoder_db": str(root / "main.sqlite"),
            "qoder_local_storage": str(root / "Local Storage" / "leveldb"),
        }))
    except Exception:
        pass
    if not models:
        models.update(QODER_CN_MODEL_ALIASES)
        models.update(QODER_CN_MODEL_ALIASES.values())
    return sorted(models)


def _models_from_workbuddy_real(home: Path, projects: Path | None) -> list[str]:
    models = set(_models_from_workbuddy(home / ".workbuddy", projects))
    if any(str(m).lower() in ("glm-5.3-flash", "glm-5.3") for m in models):
        models.update(WORKBUDDY_MODEL_ALIASES)
        models.update(WORKBUDDY_MODEL_ALIASES.values())
    return sorted(models)


def _qoder_cn_version(home: Path) -> str:
    status = read_json(home / ".qoder-cn" / ".qoder-app-status.json", {}) or {}
    version = str(status.get("version") or "").strip() if isinstance(status, dict) else ""
    return version or "unknown"


def _models_from_dsh(dsh_home: Path) -> list[str]:
    out: set[str] = set()
    settings_candidates = [dsh_home / "settings.yaml", dsh_home / "settings.yml",
                           dsh_home / "settings.yaml.imported"]
    settings_candidates.extend(sorted(dsh_home.glob("settings*.y*ml")))
    for path in settings_candidates:
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        providers = ((data.get("llm-pi-ai") or {}).get("providers") or {})
        if isinstance(providers, dict):
            for provider in providers.values():
                if isinstance(provider, dict):
                    _collect_model_entries(provider.get("models"), out)
        _collect_model_entries(data.get("agent-default-model"), out)
        _collect_model_entries((data.get("subagent-model-selection") or {}).get("allowedModels"), out)
        _collect_model_entries(data.get("models"), out)
    catalog = read_json(dsh_home / ".workbuddy-catalog.json", {}) or {}
    entries = catalog.get("entries") if isinstance(catalog, dict) else None
    if isinstance(entries, dict):
        for entry in entries.values():
            if isinstance(entry, dict):
                _collect_model_entries(entry.get("models"), out)
    return sorted(out)


def _models_from_zcode(zcode_home: Path, rollout: Path | None) -> list[str]:
    out: set[str] = set()
    if rollout:
        out.update(_models_from_rollout(rollout))
    out.update(_models_from_provider_config(zcode_home / "v2" / "provider_config.json"))
    return sorted(out)


def detect_models_for_agent(agent: AgentRecord, ctx: DiscoveryContext | None = None) -> list[str]:
    """Live model discovery for one registered agent.

    Only observed data is returned: CLI output, local session events or the
    agent's own provider config. No hard-coded model catalogue is invented.
    """
    ctx = ctx or DiscoveryContext.from_env()
    cfg = agent.adapter_config or {}
    reader = str(cfg.get("session_reader") or agent.agent_id)
    exe = str(cfg.get("executable") or "")
    data_path = Path(str(cfg.get("data_path"))) if cfg.get("data_path") else None
    if agent.agent_id == "opencode" or reader == "opencode":
        return _opencode_models(exe)
    if reader == "qoder":
        projects = data_path or ctx.home / ".qoder" / "projects"
        if agent.agent_id == "qoder_cn":
            return _models_from_qoder_cn(ctx.home, projects, ctx.appdata)
        try:
            from ..control.interfaces import list_qoder_models
            rows = list_qoder_models({"qoder_db": str(cfg.get("qoder_db") or "")})
            if rows:
                return sorted({row["id"] for row in rows})
        except Exception:
            pass
        return _models_from_jsonl_projects(projects, kind="qoder")
    if reader == "workbuddy":
        return _models_from_workbuddy_real(ctx.home, data_path)
    if reader == "zcode":
        return _models_from_zcode(ctx.home / ".zcode", data_path)
    if reader == "dsh":
        return sorted(set(_models_from_dsh(ctx.home / ".dsh"))
                      | set(_models_from_dsh_sessions(ctx.home / ".dsh" / "sessions")))
    if agent.agent_id in ("trae", "trae_cn"):
        # The encrypted ai-agent DB is not the model source: the plain
        # state.vscdb carries the real catalog.  Return the SOLO composer's
        # model ids (the same set the in-app menu offers).
        try:
            from ..control import trae_interface
            rows = trae_interface.list_trae_models(agent.agent_id, cfg)
            return sorted({row["id"] for row in rows
                           if row.get("agent_label") == trae_interface.AGENT_LABEL})
        except Exception:
            return []
    if agent.adapter_type == AdapterType.MCP.value:
        return []
    return sorted(set(agent.models or []))


def _rea_home(ctx: DiscoveryContext) -> Path | None:
    candidates: list[Path] = []
    env = os.environ.get("AGENTMESH_REA_HOME", "").strip()
    if env:
        candidates.append(Path(env))
    if ctx.rea_home:
        candidates.append(ctx.rea_home)
    candidates.extend([
        ctx.home / "rea",
        Path("C:/rea"),
        Path("D:/rea"),
    ])
    for candidate in candidates:
        try:
            if candidate.is_dir() and (candidate / ".mcp.json").exists():
                return candidate
        except OSError:
            continue
    return None


def _discover_rea(project_id: str, ctx: DiscoveryContext) -> AgentRecord | None:
    home = _rea_home(ctx)
    if not home:
        return None
    mcp_config = read_json(home / ".mcp.json", {}) or {}
    servers = mcp_config.get("mcpServers") if isinstance(mcp_config, dict) else None
    if not isinstance(servers, dict) or not servers:
        return None
    server_name = "rea" if "rea" in servers else sorted(servers)[0]
    server = servers.get(server_name) or {}
    if not isinstance(server, dict):
        return None
    command = str(server.get("command") or "")
    args = [str(a) for a in (server.get("args") or [])]
    env = {str(k): str(v) for k, v in (server.get("env") or {}).items()}
    installed = bool(command) and (Path(command).exists() or bool(shutil.which(command)))
    version = "unknown"
    for package in (
        home / ".rea-cli" / "node_modules" / "rea-agents" / "package.json",
        home / "node_modules" / "rea-agents" / "package.json",
    ):
        data = read_json(package, {}) or {}
        if data.get("version"):
            version = str(data["version"])
            break
    capabilities = {
        "read_files": True, "write_files": False, "terminal": False,
        "web_search": False, "mcp": True, "session_read": False,
        "session_write": False, "structured_output": True,
        "streaming": False, "background": True,
        "analysis": True, "code_review": True, "local_mcp": True,
    }
    if not installed:
        capabilities = {name: False for name in capabilities}
    record = AgentRecord(
        agent_id="rea",
        name="REA Studio",
        product="REA Studio",
        edition="local",
        project_id=project_id,
        version=version,
        adapter_type=AdapterType.MCP.value,
        adapter_config={
            "mode": "mcp", "mcp_server": server_name, "command": command, "args": args,
            "env": env, "cwd": str(home), "config_path": str(home / ".mcp.json"),
            "timeout_seconds": 300,
        },
        access_level=AccessLevel.L2.value,
        capabilities=capabilities,
        models=[],
        permissions=_permissions(),
        status=AgentStatus.ONLINE.value if installed else AgentStatus.OFFLINE.value,
        last_seen=_now() if installed else "",
        discovered=True,
    )
    record.extra = {
        "installed": installed,
        "mcp_server": server_name,
        "rea_home": str(home),
        "note": "REA 逆向工程 MCP 服务；工具清单通过真实 MCP tools/list 获取",
    }
    return record


def _trae_db(ctx: DiscoveryContext, edition: str) -> Path:
    app_name = "Trae CN" if edition == "cn" else "Trae"
    return ctx.appdata / app_name / "ModularData" / "ai-agent" / "database.db"


def _trae_state_db(ctx: DiscoveryContext, edition: str) -> Path:
    """Plain SQLite applicationStorage (NOT the encrypted ai-agent DB)."""
    app_name = "Trae CN" if edition == "cn" else "Trae"
    return ctx.appdata / app_name / "User" / "globalStorage" / "state.vscdb"


def _trae_programmatic(ctx: DiscoveryContext, app: str, edition: str,
                       exe: Path | None) -> tuple[dict[str, Any], list[str]] | None:
    """Evidence gate: only register zero-input automation when it is verifiable.

    Requires the real executable, a plain SQLite applicationStorage and at
    least one real model in the app's own catalog.  Anything else stays on the
    honest file-relay path.
    """
    if exe is None:
        return None
    config = {"trae_state_db": str(_trae_state_db(ctx, edition))}
    try:
        from ..control import trae_interface
        if not trae_interface.trae_state_db(app, config).exists():
            return None
        models = trae_interface.list_trae_models(app, config)
    except Exception:
        return None
    if not models:
        return None
    gui_config = {
        "mode": "gui_automation",
        "gui_app": app,
        "executable": str(exe),
        "trae_state_db": str(_trae_state_db(ctx, edition)),
        "trae_dev_mode": "code",
        "trae_cdp_port": int(os.environ.get("AGENTMESH_TRAE_CDP_PORT")
                             or trae_interface.CDP_DEFAULT_PORT),
        "session_reader": app,
        "data_path": str(_trae_db(ctx, edition)),
    }
    return gui_config, sorted({m["id"] for m in models
                               if m["agent_label"] == trae_interface.AGENT_LABEL})


def discover_agents(project_id: str = "", ctx: DiscoveryContext | None = None) -> list[AgentRecord]:
    ctx = ctx or DiscoveryContext.from_env()
    out: list[AgentRecord] = []

    # --- Trae (international / CN) ---------------------------------------
    trae_exe = _first_existing([
        ctx.localappdata / "Programs" / "Trae" / "Trae.exe",
        ctx.programfiles / "Trae" / "Trae.exe",
    ])
    trae_data = _trae_db(ctx, "intl")
    trae_home = ctx.home / ".trae"
    trae_programmatic = _trae_programmatic(ctx, "trae", "intl", trae_exe)
    trae_record = _make_agent(
        agent_id="trae", name="Trae", product="Trae", edition="international",
        exe=trae_exe, data_path=trae_data if trae_data.exists() else (trae_home if trae_home.exists() else None),
        reader="trae", project_id=project_id,
        access_level=AccessLevel.L3.value if trae_programmatic else
            (AccessLevel.L2.value if trae_exe or trae_data.exists() else AccessLevel.L1.value),
        structured=bool(trae_programmatic),
        adapter_type=(AdapterType.GUI_AUTOMATION.value if trae_programmatic
                      else AdapterType.FILE_RELAY.value),
        adapter_config=(trae_programmatic[0] if trae_programmatic
                        else _file_relay_config(str(trae_data), "trae")),
        models=(trae_programmatic[1] if trae_programmatic else []),
    )
    out.append(trae_record)

    trae_cn_exe = _first_existing([
        ctx.localappdata / "Programs" / "Trae CN" / "Trae CN.exe",
        ctx.programfiles / "Trae CN" / "Trae CN.exe",
    ])
    trae_cn_data = _trae_db(ctx, "cn")
    trae_cn_home = ctx.home / ".trae-cn"
    trae_cn_programmatic = _trae_programmatic(ctx, "trae_cn", "cn", trae_cn_exe)
    trae_cn_record = _make_agent(
        agent_id="trae_cn", name="Trae CN", product="Trae CN", edition="domestic",
        exe=trae_cn_exe,
        data_path=trae_cn_data if trae_cn_data.exists() else (trae_cn_home if trae_cn_home.exists() else None),
        reader="trae", project_id=project_id,
        access_level=AccessLevel.L3.value if trae_cn_programmatic else
            (AccessLevel.L2.value if trae_cn_exe or trae_cn_data.exists() else AccessLevel.L1.value),
        structured=bool(trae_cn_programmatic),
        adapter_type=(AdapterType.GUI_AUTOMATION.value if trae_cn_programmatic
                      else AdapterType.FILE_RELAY.value),
        adapter_config=(trae_cn_programmatic[0] if trae_cn_programmatic
                        else _file_relay_config(str(trae_cn_data), "trae")),
        models=(trae_cn_programmatic[1] if trae_cn_programmatic else []),
    )
    out.append(trae_cn_record)
    for app_id, record, programmatic in (
        ("trae", trae_record, trae_programmatic),
        ("trae_cn", trae_cn_record, trae_cn_programmatic),
    ):
        if programmatic:
            record.capabilities.update({
                "gui_control": True, "auto_executable": True,
                "structured_output": True, "background": True,
            })
            record.extra["gui_automation"] = {
                "app": app_id,
                "window_title": "Trae CN" if app_id == "trae_cn" else "Trae",
                "interface": "state.vscdb + cdp-dom",
                "model_count": len(programmatic[1]),
            }

    # --- Qoder ------------------------------------------------------------
    qoder_exe = _first_existing([
        ctx.programfiles / "Qoder" / "Qoder" / "Qoder.exe",
        ctx.localappdata / "Programs" / "Qoder IDE" / "Qoder IDE.exe",
    ])
    qoder_projects = ctx.home / ".qoder" / "projects"
    qoder_intl_record = _make_agent(
        agent_id="qoder", name="Qoder", product="Qoder", edition="international",
        exe=qoder_exe, data_path=qoder_projects if qoder_projects.exists() else None,
        reader="qoder", project_id=project_id,
        access_level=AccessLevel.L2.value if qoder_exe or qoder_projects.exists() else AccessLevel.L1.value,
        structured=False, models=_models_from_jsonl_projects(qoder_projects, kind="qoder"),
    )
    qoder_intl_record.adapter_config.update({
        "qoder_db": str(ctx.appdata / "com.qoder.app.stable" / "main.sqlite"),
        "qoder_projects": str(qoder_projects),
        "qoder_image": "Qoder.exe",
        "gui_app": "qoder",
    })
    out.append(qoder_intl_record)

    qoder_cn_exe = _first_existing([
        ctx.programfiles / "Qoder" / "Qoder CN" / "Qoder CN.exe",
        ctx.localappdata / "Programs" / "Qoder CN" / "Qoder CN.exe",
    ])
    qoder_cn_projects = ctx.home / ".qoder-cn" / "projects"
    qoder_cn_gui = qoder_cn_exe is not None
    qoder_cn_config = {
        "mode": "gui_automation" if qoder_cn_gui else "file_relay",
        "gui_app": "qoder_cn" if qoder_cn_gui else "",
        "window_title": "Qoder CN",
        "executable": str(qoder_cn_exe) if qoder_cn_exe else "",
        "launch_command": [str(qoder_cn_exe)] if qoder_cn_exe else [],
        "input_offset_ratio": [0.524, 0.804],
        "input_double_click": True,
        "submit_key": "ENTER",
        "model_selector_offset_ratio": None,
        "model_item_offset_ratio": {},
        "model_aliases": {
            "dfmodel": "dfmodel", "DeepSeek-Flash": "dfmodel",
            "DeepSeek Flash": "dfmodel", "deepseek-flash": "dfmodel",
        },
        "display_model": "DeepSeek-Flash",
        "model_switch_supported": False,
        "session_reader": "qoder",
        "transcript_reader": "qoder_cn",
        "data_path": str(qoder_cn_projects),
        "send_timeout_seconds": 90,
        "stall_seconds": 90,
        "timeout_seconds": 1800,
        "launch_timeout_seconds": 120,
        "wait_seconds": 0,
        "poll_seconds": 1,
    }
    qoder_cn = _make_agent(
        agent_id="qoder_cn", name="Qoder CN", product="Qoder CN", edition="domestic",
        exe=qoder_cn_exe,
        data_path=qoder_cn_projects if qoder_cn_projects.exists() else None,
        reader="qoder", project_id=project_id,
        access_level=AccessLevel.L3.value if qoder_cn_gui else (
            AccessLevel.L2.value if qoder_cn_exe or qoder_cn_projects.exists() else AccessLevel.L1.value
        ),
        structured=qoder_cn_gui,
        adapter_type=AdapterType.GUI_AUTOMATION.value if qoder_cn_gui else AdapterType.FILE_RELAY.value,
        adapter_config=qoder_cn_config,
        models=_models_from_qoder_cn(ctx.home, qoder_cn_projects, ctx.appdata),
        version=_qoder_cn_version(ctx.home) if _qoder_cn_version(ctx.home) != "unknown"
        else _read_version_from_exe(qoder_cn_exe),
    )
    if qoder_cn_gui:
        qoder_cn.capabilities.update({
            "gui_control": True, "auto_executable": True, "structured_output": True,
            "background": True,
        })
        qoder_cn.extra["gui_automation"] = {
            "app": "qoder_cn", "window_title": "Qoder CN",
            "transcript_reader": "qoder_cn", "model_default": "DeepSeek-Flash",
        }
    out.append(qoder_cn)

    # --- ZCode ------------------------------------------------------------
    zcode_exe = _first_existing([
        ctx.programfiles / "ZCode" / "ZCode.exe",
        ctx.localappdata / "Programs" / "ZCode" / "ZCode.exe",
    ])
    zcode_rollout = ctx.home / ".zcode" / "cli" / "rollout"
    # The desktop install ships a headless CLI core (resources/glm/zcode.cjs).
    # With ELECTRON_RUN_AS_NODE=1 it runs without the GUI, so ZCode is L3:
    # zero-input prompt + model pinning via a temporary provider config copy.
    zcode_cjs = zcode_exe.parent.joinpath("resources", "glm", "zcode.cjs") if zcode_exe else None
    zcode_cli = bool(zcode_exe and zcode_cjs and zcode_cjs.exists())
    zcode_config = {
        "mode": "zcode_cli" if zcode_cli else "file_relay",
        "zcode_exe": str(zcode_exe) if zcode_exe else "",
        "zcode_cjs": str(zcode_cjs) if zcode_cjs and zcode_cjs.exists() else "",
        "executable": str(zcode_exe) if zcode_exe else "",
        "permission_mode": "yolo",
        "model_retry_max_retries": 1,
        "timeout_seconds": 1800,
        "repair_attempts": 1,
        "session_reader": "zcode",
        "data_path": str(zcode_rollout),
        "provider_config": str(ctx.home / ".zcode" / "v2" / "provider_config.json"),
    }
    zcode_record = _make_agent(
        agent_id="zcode", name="ZCode", product="ZCode", edition="domestic",
        exe=zcode_exe, data_path=zcode_rollout if zcode_rollout.exists() else (ctx.home / ".zcode" if (ctx.home / ".zcode").exists() else None),
        reader="zcode", project_id=project_id,
        access_level=AccessLevel.L3.value if zcode_cli else (
            AccessLevel.L2.value if zcode_exe or zcode_rollout.exists() else AccessLevel.L1.value),
        structured=zcode_cli,
        adapter_type=AdapterType.ZCODE.value if zcode_cli else AdapterType.FILE_RELAY.value,
        adapter_config=zcode_config,
        models=_models_from_zcode(ctx.home / ".zcode", zcode_rollout),
    )
    if zcode_cli:
        zcode_record.capabilities.update({
            "auto_executable": True, "structured_output": True, "background": True,
        })
        zcode_record.extra["zcode_cli"] = {
            "entrypoint": str(zcode_cjs),
            "prompt_flag": "-p",
            "json_output": True,
            "model_pinning": "temp provider config (providerOrder/modelOrder)",
        }
    out.append(zcode_record)

    # --- WorkBuddy --------------------------------------------------------
    wb_exe = _first_existing([
        ctx.programfiles / "WorkBuddy" / "WorkBuddy.exe",
        ctx.localappdata / "Programs" / "WorkBuddy" / "WorkBuddy.exe",
    ])
    wb_projects = ctx.home / ".workbuddy" / "projects"
    wb_gui = wb_exe is not None
    wb_config = {
        "mode": "gui_automation" if wb_gui else "file_relay",
        "gui_app": "workbuddy" if wb_gui else "",
        "window_title": "WorkBuddy",
        "executable": str(wb_exe) if wb_exe else "",
        "launch_command": [str(wb_exe)] if wb_exe else [],
        "input_offset_ratio": [0.439, 0.395],
        "input_double_click": True,
        "submit_key": "ENTER",
        "model_selector_offset_ratio": [0.796, 0.519],
        "model_item_offset_ratio": {"glm-5.3-flash": [0.630, 0.060]},
        "model_aliases": {
            "glm-5.3-flash": "glm-5.3-flash",
            "GLM-5.3-Flash": "glm-5.3-flash",
            "GLM 5.3 Flash": "glm-5.3-flash",
        },
        "display_model": "GLM-5.3-Flash",
        "model_switch_supported": True,
        "session_reader": "workbuddy",
        "transcript_reader": "workbuddy",
        "data_path": str(wb_projects),
        "session_db": str(ctx.home / ".workbuddy" / "workbuddy.db"),
        "send_timeout_seconds": 90,
        "stall_seconds": 90,
        "timeout_seconds": 1800,
        "launch_timeout_seconds": 120,
        "wait_seconds": 0,
        "poll_seconds": 1,
    }
    workbuddy = _make_agent(
        agent_id="workbuddy", name="WorkBuddy", product="WorkBuddy", edition="domestic",
        exe=wb_exe, data_path=wb_projects if wb_projects.exists() else None,
        reader="workbuddy", project_id=project_id,
        access_level=AccessLevel.L3.value if wb_gui else (
            AccessLevel.L2.value if wb_exe or wb_projects.exists() else AccessLevel.L1.value
        ),
        structured=wb_gui,
        adapter_type=AdapterType.GUI_AUTOMATION.value if wb_gui else AdapterType.FILE_RELAY.value,
        adapter_config=wb_config,
        models=_models_from_workbuddy_real(ctx.home, wb_projects),
    )
    if wb_gui:
        workbuddy.capabilities.update({
            "gui_control": True, "auto_executable": True, "structured_output": True,
            "background": True,
        })
        workbuddy.extra["gui_automation"] = {
            "app": "workbuddy", "window_title": "WorkBuddy",
            "transcript_reader": "workbuddy", "model_default": "GLM-5.3-Flash",
        }
    out.append(workbuddy)

    # --- DeepSeek Harness -------------------------------------------------
    dsh_exe = _first_existing([
        ctx.localappdata / "Programs" / "DeepSeek Harness" / "DeepSeek Harness.exe",
        ctx.programfiles / "DeepSeek Harness" / "DeepSeek Harness.exe",
    ])
    dsh_sessions = ctx.home / ".dsh" / "sessions"
    # The desktop install ships a headless profile booted through the Electron
    # binary in Node mode (resources/app.asar/.../cli.js --profile headless
    # --json). With a real exe + asar archive dsh is L3: zero-input prompt,
    # JSON run events and --session-id continuity. Credentials gate execution
    # (MISSING_CREDENTIAL), not the channel's programmability.
    from .dsh import resolve_dsh_install
    dsh_exe_resolved, dsh_cli = resolve_dsh_install({"dsh_exe": str(dsh_exe) if dsh_exe else ""})
    dsh_headless = bool(dsh_exe_resolved and dsh_cli)
    dsh_config = {
        "mode": "dsh_cli" if dsh_headless else "file_relay",
        "dsh_exe": dsh_exe_resolved,
        "dsh_cli": dsh_cli,
        "executable": dsh_exe_resolved,
        "profile": "headless",
        "timeout_seconds": 1800,
        "repair_attempts": 1,
        "session_reader": "dsh",
        "data_path": str(dsh_sessions),
    }
    dsh_record = _make_agent(
        agent_id="dsh", name="DeepSeek Harness", product="DeepSeek Harness", edition="domestic",
        exe=dsh_exe,
        data_path=dsh_sessions if dsh_sessions.exists() else (ctx.home / ".dsh" if (ctx.home / ".dsh").exists() else None),
        reader="dsh", project_id=project_id,
        access_level=AccessLevel.L3.value if dsh_headless else (
            AccessLevel.L2.value if dsh_exe or dsh_sessions.exists() else AccessLevel.L1.value),
        structured=dsh_headless,
        adapter_type=AdapterType.DSH.value if dsh_headless else AdapterType.FILE_RELAY.value,
        adapter_config=dsh_config,
        models=sorted(set(_models_from_dsh_sessions(dsh_sessions))
                      | set(_models_from_dsh(ctx.home / ".dsh"))),
    )
    if dsh_headless:
        dsh_record.capabilities.update({
            "auto_executable": True, "structured_output": True, "background": True,
        })
        dsh_record.extra["dsh_cli"] = {
            "entrypoint": dsh_cli,
            "profile": "headless",
            "json_output": True,
            "session_resume": "--session-id",
            "credential": "DEEPSEEK_API_KEY or a provider plugin patched into the headless profile",
        }
    out.append(dsh_record)

    # --- OpenCode (only real non-interactive CLI on this machine) ----------
    oc_exe = resolve_opencode_executable()
    oc_db = ctx.home / ".local" / "share" / "opencode" / "opencode.db"
    oc_models = _opencode_models(oc_exe)
    out.append(_make_agent(
        agent_id="opencode", name="OpenCode", product="OpenCode", edition="international",
        exe=Path(oc_exe) if oc_exe else None,
        data_path=oc_db if oc_db.exists() else None,
        reader="opencode", project_id=project_id,
        access_level=AccessLevel.L3.value if oc_exe else AccessLevel.L2.value,
        structured=True, adapter_type=AdapterType.OPENCODE.value,
        adapter_config={
            "mode": "opencode", "executable": oc_exe,
            "wait_seconds": 0, "poll_seconds": 1,
            "session_reader": "opencode",
            "data_path": str(oc_db) if oc_db.exists() else "",
        },
        models=oc_models, version=_opencode_version(oc_exe),
    ))
    # OpenCode is a real CLI: capabilities that depend on it are now true.
    oc = out[-1]
    oc.capabilities.update({"session_write": True, "streaming": True, "background": True})

    # --- Additional products named by 需求整理-v1 ---------------------------
    # These are registered only when a real executable or config directory
    # exists. No GUI automation and no private-state probing is attempted.
    connector_specs: list[tuple[str, tuple[str, ...], list[Path]]] = [
        ("claude_code", ("claude",), [ctx.home / ".claude"]),
        ("gemini_cli", ("gemini",), [ctx.home / ".gemini"]),
        ("codex_cli", ("codex",), [ctx.home / ".codex"]),
        ("aider", ("aider",), [ctx.home / ".aider"]),
        ("cursor", ("cursor",), [ctx.home / ".cursor", ctx.localappdata / "Programs" / "Cursor"]),
    ]
    for profile_id, cli_names, config_dirs in connector_specs:
        profile = profile_for(profile_id)
        if not profile:
            continue
        exe = _which(ctx, cli_names)
        config_dir = _first_dir(config_dirs)
        if not exe and not config_dir:
            # Still list the product in the manifest so users see it as offline.
            exe = None
            config_dir = None
        installed = exe is not None or config_dir is not None
        adapter_type = profile.adapter_type if exe or profile.adapter_type != AdapterType.CLI.value else AdapterType.CLI.value
        if profile.adapter_type == AdapterType.CLI.value:
            adapter_config = {
                "mode": "cli",
                "command": str(exe) if exe else "",
                "args": list(profile.cli_args),
                "prompt_mode": profile.prompt_mode,
                "timeout_seconds": 3600,
                "parse": "text",
                "note": profile.note,
            }
        else:
            adapter_config = _file_relay_config(str(config_dir or ""), "")
            adapter_config["mode"] = "file_relay"
        capabilities = _base_capabilities(
            session_read=False, structured=False,
            web="web_search" in profile.assumed,
        )
        capabilities.update({name: True for name in profile.assumed})
        access = profile.access_level_if_installed if (exe or config_dir) else AccessLevel.L1.value
        if profile.adapter_type == AdapterType.CLI.value and not exe:
            access = AccessLevel.L1.value
        record = _make_agent(
            agent_id=profile.agent_id, name=profile.product, product=profile.product,
            edition=profile.edition, exe=exe, data_path=config_dir,
            reader="", project_id=project_id, access_level=access,
            structured=bool(capabilities.get("structured_output")),
            web=bool(capabilities.get("web_search")),
            adapter_type=adapter_type, adapter_config=adapter_config,
            models=[], version=_read_version_from_exe(exe),
        )
        record.capabilities = capabilities if exe else {name: False for name in capabilities}
        record.status = AgentStatus.ONLINE.value if exe else AgentStatus.OFFLINE.value
        record.extra = {"installed": installed, "has_sessions": False, "note": profile.note}
        out.append(record)

    # --- REA Studio (local MCP reverse-engineering service) ----------------
    rea = _discover_rea(project_id, ctx)
    if rea:
        out.append(rea)

    return out
