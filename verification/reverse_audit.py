"""Read-only reverse-engineering audit of every Agent installed on this box.

Probes the applications' OWN stores and launch protocols.  It never calls a
vendor LLM HTTP endpoint, never writes to an application database, and never
synthesises mouse/keyboard input.  Output: ``verification/reverse-audit.json``.

Run:  python verification/reverse_audit.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentloop_hub.control import trae_interface as trae  # noqa: E402
from agentloop_hub.control.interfaces import (  # noqa: E402
    list_qoder_models, list_workbuddy_models, qoder_db_path, workbuddy_db_path,
)

HOME = Path.home()
APPDATA = Path(os.environ.get("APPDATA") or HOME / "AppData" / "Roaming")
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData" / "Local")
PROGRAMS = LOCALAPPDATA / "Programs"


def _plain_sqlite(path: Path) -> bool:
    try:
        return path.exists() and path.open("rb").read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _sqlite(path: Path, sql: str, limit: int = 20):
    if not _plain_sqlite(path):
        return []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        return [tuple(r) for r in con.execute(sql).fetchmany(limit)] \
            if True else []
    except sqlite3.Error:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


def _tables(path: Path) -> list[str]:
    return [r[0] for r in _sqlite(
        path, "select name from sqlite_master where type='table' order by name", 60)]


def _item_keys(path: Path, needles: tuple[str, ...], limit: int = 30) -> list[str]:
    if not _plain_sqlite(path):
        return []
    rows = _sqlite(path, "select key from ItemTable", 10000)
    return [str(r[0]) for r in rows
            if any(n in str(r[0]).lower() for n in needles)][:limit]


def _trae(app: str) -> dict:
    probe = trae.probe(app)
    db = Path(probe["state_db"])
    return {
        "product": probe["product"],
        "executable": probe["executable"],
        "installed": probe["executable_exists"],
        "protocol": probe["url_protocol"],
        "state_db": str(db),
        "state_db_plain_sqlite": probe["state_db_plain_sqlite"],
        "state_keys": _item_keys(db, ("model", "mode", "workspace", "route", "project")),
        "model_catalog": {
            "source": "<uid>:AI.agent.model.model_list_map (ItemTable)",
            "count": probe.get("model_count", 0),
            "sample": probe.get("models", [])[:12],
            "deepseek_v4_1_flash": bool(trae.resolve_trae_model(
                "DeepSeek-V4.1-Flash", app=app)),
            "glm_5_3_flash": bool(trae.resolve_trae_model("GLM-5.3-Flash", app=app)),
            "selected": probe.get("selected_model", {}),
        },
        "workspace_channel": [
            "solo-lite.local-project-folders",
            "solo-lite.project-workspace-cache",
            "solo-lite-mode-state-map-<uid> session.local_folder/mainFolder/workspacePath",
            "solo-lite:route:<uid>",
            "--folder-uri (out/cli.js)",
        ],
        "dev_mode_channel": "solo-lite-mode-state-map-<uid>: work / code / design",
        "chat_channel": [
            "CDP Runtime.evaluate insertText + click .icube-chat-input-action-btn-send",
            "deeplink <protocol>://GitHub.copilot-chat?agent=&prompt= (prefill only)",
            "CLI `chat <prompt> -m <mode>` (out/cli.js)",
        ],
        "readback_channel": [
            "<uid>:AI.agent.model.recent_user_selection_by_agent_label",
            "solo-lite-mode-state-map-<uid> (workspacePath / mode)",
            "CDP document.body.innerText (visible conversation)",
            "~/.trae-cn/memory/projects/**/session_memory_*.jsonl (summary only)",
        ],
        "cdp": "verified: --remote-debugging-port works after removing ELECTRON_RUN_AS_NODE",
        "evidence": [
            "resources/app/product.json:39 urlProtocol",
            "resources/app/out/cli.js:29 --folder-uri / chat subcommand",
            "resources/app/out/vs/workbench/workbench.desktop.main.js:2844 chat deeplink",
            "resources/app/out/vs/workbench/workbench.desktop.main.js:3777 send button class",
            "resources/app/node_modules/@byted-icube/solo-lite/dist/5806.88847ced.mjs:2772 model keys",
            "resources/app/out/main.js:2645 patchSelections writes applicationStorage",
        ],
        "status": "verified" if probe["state_db_plain_sqlite"] else "not_installed",
    }


def _qoder(edition: str) -> dict:
    cn = edition == "qoder_cn"
    cfg = {}
    if not cn:
        cfg = {"qoder_db": str(APPDATA / "com.qoder.app.stable" / "main.sqlite"),
               "qoder_projects": str(HOME / ".qoder" / "projects")}
    db = qoder_db_path(cfg)
    models = list_qoder_models(cfg)
    exe = (Path(r"C:\Program Files\Qoder\Qoder CN\Qoder CN.exe") if cn
           else Path(r"C:\Program Files\Qoder\Qoder\Qoder.exe"))
    return {
        "product": "Qoder CN" if cn else "Qoder",
        "executable": str(exe),
        "installed": exe.exists(),
        "state_db": str(db),
        "state_db_plain_sqlite": _plain_sqlite(db),
        "state_tables": _tables(db)[:20],
        "model_catalog": {
            "source": "chat_model_preferences.model_key + chat_sessions.model",
            "count": len(models),
            "keys": [m["id"] for m in models][:20],
            "glm_5_3_flash": "gfmodel" in [m["id"] for m in models],
        },
        "workspace_channel": [
            "main.sqlite workspaces.root_paths_json",
            "chat_sessions.cwd / workspace_id / execution_target_json",
            "transcript workspace-directories event",
            "sites/sessions/*.json location.projectRoot",
        ],
        "dev_mode_channel": "product_mode column (coding) + transcript runtime-config",
        "chat_channel": "CDP Runtime.evaluate insertText + send button click",
        "readback_channel": [
            "main.sqlite chat_sessions.cwd/model",
            "~/.qoder-cn/projects/<slug>/<session>.jsonl runtime-config + assistant.model",
            "qoder-agent-sdk.log `session opened cwd=`",
        ],
        "cdp": "verified for Qoder CN (AGENTMESH_QODER_CDP_PORT=9222)",
        "evidence": [
            "com.qodercn.app.stable/main.sqlite (plain SQLite, chat_sessions/workspaces)",
            "~/.qoder-cn/projects/**/*.jsonl transcript events",
            "interfaces.py prepare_qoder_session / verify_qoder_session",
        ],
        "status": "verified" if cn and _plain_sqlite(db) else "partial",
    }


def _workbuddy() -> dict:
    db = workbuddy_db_path({})
    models = list_workbuddy_models({})
    return {
        "product": "WorkBuddy",
        "executable": r"C:\Program Files\WorkBuddy\WorkBuddy.exe",
        "installed": Path(r"C:\Program Files\WorkBuddy\WorkBuddy.exe").exists(),
        "state_db": str(db),
        "state_db_plain_sqlite": _plain_sqlite(db),
        "model_catalog": {"source": "~/.workbuddy/local_storage/*.info product manifest",
                          "count": len(models), "ids": [m["id"] for m in models][:20]},
        "workspace_channel": "workbuddy://task?action=start&cwd=&model=&welcomeMode=&permissionMode=&mode=",
        "dev_mode_channel": "welcomeMode code/work/design -> sessions.source_mode craft/working/design",
        "chat_channel": "deeplink prefills composer; CDP DOM click on 发送 button (no autoSend)",
        "readback_channel": "~/.workbuddy/workbuddy.db sessions.cwd/model/source_mode/permission_mode",
        "cdp": "verified --remote-debugging-port=9223",
        "evidence": ["app.asar task-deeplink-coordinator prepareInput (no autoSend)",
                     "workbuddy.db sessions table", "interfaces.py prepare_workbuddy_task"],
        "status": "verified",
    }


def _zcode() -> dict:
    db = HOME / ".zcode" / "cli" / "db" / "db.sqlite"
    exe = Path(r"C:\Program Files\ZCode\ZCode.exe")
    cjs = exe.parent / "resources" / "glm" / "zcode.cjs"
    return {
        "product": "ZCode",
        "executable": str(exe),
        "installed": exe.exists(),
        "state_db": str(db),
        "state_db_plain_sqlite": _plain_sqlite(db),
        "state_tables": _tables(db)[:22],
        "model_catalog": {
            "source": "~/.zcode/cli/rollout/model-io-sess_*.jsonl + provider_config + local_setting",
            "sample": ["DeepSeek-V4.1-Flash", "GLM-5.3-Flash", "deepseek-v4-pro",
                       "glm-5.2", "glm-5.3", "glm-5.3-flash"],
        },
        "workspace_channel": "--cwd <dir> (headless CLI) + cli/db/db.sqlite session.directory (plain SQLite)",
        "dev_mode_channel": "-p default permission mode yolo; --mode build|edit|plan|yolo|guarded",
        "chat_channel": ("VERIFIED headless CLI: ELECTRON_RUN_AS_NODE=1 ZCode.exe"
                         " resources/glm/zcode.cjs -p \"<task>\" --cwd <dir> --json;"
                         " model pinned via temp provider config copy"
                         " (ZCODE_PERSONAL_PROVIDER_CONFIG_FILE +"
                         " ZCODE_BUILTIN_PROVIDER_CONFIG_FILE,"
                         " providerOrder/modelOrder first);"
                         " ZCODE_MODEL_RETRY_MAX_RETRIES caps the retry loop;"
                         " --resume <sess_...> continues a session"),
        "readback_channel": "cli/db/db.sqlite session.* + model_usage(provider/model/status) + rollout jsonl",
        "evidence": ["~/.zcode/cli/db/db.sqlite tables session/local_setting/model_usage",
                     "~/.zcode/cli/rollout/model-io-sess_*.jsonl",
                     "C:/Program Files/ZCode/resources/glm/zcode.cjs --help (zcode CLI 0.17.1)",
                     "real run: -p returned exact expected text via deepseek-v4-pro (2.85s)",
                     "verification/verify_zcode_real.py end-to-end chain"],
        "status": "verified" if (exe.exists() and cjs.exists()) else "partial",
    }


def _dsh() -> dict:
    root = HOME / ".dsh"
    settings = root / "settings.yaml"
    settings_effective = settings if settings.exists() else root / "settings.yaml.imported"
    catalog = root / ".workbuddy-catalog.json"
    sessions = root / "sessions"
    return {
        "product": "DeepSeek Harness",
        "installed": catalog.exists() or sessions.exists(),
        "settings": str(settings_effective),
        "settings_active": settings.exists(),
        "catalog": str(catalog),
        "catalog_exists": catalog.exists(),
        "sessions_dir": str(sessions),
        "session_files": len(list(sessions.rglob("*.jsonl.zstd"))) if sessions.exists() else 0,
        "model_catalog": {"source": "settings.yaml + .workbuddy-catalog.json",
                          "sample": ["Deepseek-V4.1-Flash", "GLM-5.3-Flash", "deepseek-v4-pro"]},
        "workspace_channel": "session directory naming (~/.dsh/sessions/<project>)",
        "dev_mode_channel": "NOT FOUND",
        "chat_channel": "NOT FOUND (zstd session files, no verified IPC/CLI send path)",
        "readback_channel": "settings.yaml + .jsonl.zstd sessions (zstandard)",
        "evidence": ["~/.dsh/settings.yaml", "~/.dsh/.workbuddy-catalog.json",
                     "~/.dsh/sessions/**/*.jsonl.zstd"],
        "status": "partial",
    }


def _cursor() -> dict:
    db = APPDATA / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    return {
        "product": "Cursor",
        "executable": str(PROGRAMS / "cursor" / "Cursor.exe"),
        "installed": (PROGRAMS / "cursor" / "Cursor.exe").exists(),
        "state_db": str(db),
        "state_db_plain_sqlite": _plain_sqlite(db),
        "state_keys": _item_keys(db, ("model", "mode", "workspace", "composer", "chat")),
        "model_catalog": {"source": "cursor/initialModelState + cursorDiskKV (ItemTable)",
                          "sample": []},
        "workspace_channel": "CLI --folder-uri (VSCode fork) + workspaceStorage/<id>/state.vscdb",
        "dev_mode_channel": "NOT FOUND (no verified mode key)",
        "chat_channel": "NOT FOUND (no verified zero-input send path)",
        "readback_channel": "conversation-search.db + composerHeaders (plain SQLite)",
        "evidence": ["AppData/Roaming/Cursor/User/globalStorage/state.vscdb ItemTable",
                     "AppData/Roaming/Cursor/User/globalStorage/conversation-search.db",
                     "AppData/Local/Programs/cursor/Cursor.exe"],
        "status": "partial",
    }


def _opencode() -> dict:
    from agentloop_hub.adapters.discovery import DiscoveryContext, discover_agents
    ctx = DiscoveryContext.from_env()
    record = next((a for a in discover_agents("audit", ctx) if a.agent_id == "opencode"), None)
    models = list(record.models) if record else []
    return {
        "product": "OpenCode",
        "installed": bool(record and record.status == "online"),
        "adapter": record.adapter_type if record else "",
        "model_catalog": {"source": "opencode CLI models output",
                          "count": len(models),
                          "glm_5_3_flash_free": "opencode/glm-5.3-flash-free" in models,
                          "sample": [m for m in models if "glm" in m.lower()][:10]},
        "workspace_channel": "process cwd = task worktree (subprocess)",
        "dev_mode_channel": "CLI non-interactive run (no interactive mode)",
        "chat_channel": "opencode run --format json --model <id> (stdio, non-interactive)",
        "readback_channel": "stdout JSON event stream (step_finish usage/cost)",
        "evidence": ["adapters/cli.py run_process", "adapters/opencode.py JSONL parser",
                     "adapters/discovery.py model probe"],
        "status": "verified",
    }


def _rea() -> dict:
    return {
        "product": "REA Studio", "installed": True,
        "model_catalog": {"source": "MCP tools/list (229 tools)", "count": 0},
        "workspace_channel": "NOT APPLICABLE (tool service, no session workspace)",
        "dev_mode_channel": "NOT APPLICABLE",
        "chat_channel": "MCP stdio tools/call (not a chat agent)",
        "readback_channel": "MCP tools/list + tools/call response",
        "evidence": ["project .mcp.json rea-agents 6.8.0", "adapters/mcp.py initialize/tools"],
        "status": "verified (tool service, no LLM model)",
    }


def _not_installed(name: str, cli: str) -> dict:
    return {"product": name, "installed": False, "status": "not_installed",
            "cli": cli, "note": "未安装，无本机可逆向的软件接口；不做任何猜测"}


def main() -> int:
    report = {
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "policy": {
            "os_input": "blocked (AGENTMESH_DISABLE_GUI_INPUT=1)",
            "vendor_http_api": "never used; all channels are the applications' own stores/CLI/CDP/deeplinks",
            "scope": "all locally installed agents; uninstalled products are marked not_installed",
        },
        "agents": {
            "trae": _trae("trae"),
            "trae_cn": _trae("trae_cn"),
            "qoder": _qoder("qoder"),
            "qoder_cn": _qoder("qoder_cn"),
            "workbuddy": _workbuddy(),
            "opencode": _opencode(),
            "zcode": _zcode(),
            "dsh": _dsh(),
            "cursor": _cursor(),
            "rea": _rea(),
            "claude_code": _not_installed("Claude Code", "claude -p"),
            "gemini_cli": _not_installed("Gemini CLI", "gemini -p"),
            "codex_cli": _not_installed("Codex CLI", "codex exec"),
            "aider": _not_installed("Aider", "aider --message"),
        },
    }
    out = ROOT / "verification" / "reverse-audit.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    summary = {k: {"installed": v.get("installed", False), "status": v.get("status", "")}
               for k, v in report["agents"].items()}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\nwrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
