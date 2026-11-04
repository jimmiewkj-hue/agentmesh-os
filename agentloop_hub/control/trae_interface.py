"""Trae / Trae CN programmatic software interface (zero OS input).

Reverse-engineered on this machine on 2026-11-04.  Every channel below is a
write to the application's own store or a launch protocol the application
implements; no vendor LLM HTTP API is called and no mouse/keyboard event is
synthesised.

Evidence (verified by reading the installed application, not by guessing):

* ``product.json`` -> ``urlProtocol="trae-cn"`` (CN) / ``"trae"`` (international),
  ``dataFolderName=".trae-cn"`` / ``".trae"``, ``deeplinkHost="trae.ai-ide"``.
* ``out/cli.js`` -> CLI options ``--folder-uri``, ``--new-window``,
  ``--user-data-dir`` and a ``chat`` subcommand (``-m/--mode``, ``-a/--add-file``).
* ``User/globalStorage/state.vscdb`` is a **plain SQLite** ``ItemTable`` (the
  encrypted file is ``ModularData/ai-agent/database.db``, which we never touch).
  Keys (``<uid>`` is the logged-in account id, e.g. ``3059310181236400``):
    - ``<uid>:AI.agent.model.model_list_map``  -> real model catalog by agent label
    - ``<uid>:AI.agent.model.recent_user_selection_by_agent_label``
        -> authoritative selection for a NEW session,
           ``{"solo_agent":{"modelId":"solo_agent_1__deepseek-v4.1-flash","mode":0}}``
    - ``<uid>:AI.agent.model.session_selected_model``
        -> per-session selection, ``{"<sessionId>":{"solo_agent":{...}}}``
    - ``<uid>_AI.agent.model.selected_model`` -> full selected model object
    - ``solo-lite-mode-state-map-<uid>`` -> dev modes ``work`` / ``code`` /
      ``design``, each holding an optional session with ``local_folder``,
      ``mainFolder``, ``workspacePath`` and ``mode``
    - ``solo-lite.local-project-folders`` / ``solo-lite.project-workspace-cache``
      / ``solo-lite:route:<uid>`` / ``<uid>:workspace-selector.recent.v1``
* ``out/vs/workbench/workbench.desktop.main.js:2844`` -> chat deeplink
  ``<protocol>://GitHub.copilot-chat?agent=<mode>&prompt=<text>`` executes
  ``workbench.action.chat.triggerSetup`` with ``{inputValue: prompt}`` (prefill).
* ``.../workbench.desktop.main.js:3777`` -> send button class
  ``icube-chat-input-action-btn-send``; the generic CDP DOM submit is used to
  click the application's own button (no OS input).
* CDP: launching ``Trae CN.exe`` with ``--remote-debugging-port`` works once the
  inherited ``ELECTRON_RUN_AS_NODE`` / ``NODE_OPTIONS`` are removed from the
  child environment (verified: ``Chrome/142.0.7444.235``, Electron 39.2.7).

Model ids are composed from the *real* catalog entry as
``<agentLabel>_<config_source>__<name>``; unknown models are rejected instead
of being invented.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..util import ensure_dir, now_iso
from .interfaces import InterfaceOutcome

APP_VARIANTS: dict[str, dict[str, str]] = {
    "trae_cn": {
        "product": "Trae CN",
        "user_data": "Trae CN",
        "home": ".trae-cn",
        "protocol": "trae-cn",
        "image": "Trae CN.exe",
    },
    "trae": {
        "product": "Trae",
        "user_data": "Trae",
        "home": ".trae",
        "protocol": "trae",
        "image": "Trae.exe",
    },
}

AGENT_LABEL = "solo_agent"
DEV_MODES = ("work", "code", "design")
MODEL_MODE = 0  # value observed in the real store; validated [0,1,2] by the app
CDP_DEFAULT_PORT = 9342


def _home() -> Path:
    return Path.home()


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or _home() / "AppData" / "Roaming")


def _local_programs() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or _home() / "AppData" / "Local") / "Programs"


def variant(app: str) -> dict[str, str]:
    return APP_VARIANTS.get(str(app or "").strip().lower(), APP_VARIANTS["trae_cn"])


def trae_executable(app: str = "trae_cn", config: dict[str, Any] | None = None) -> Path:
    configured = str((config or {}).get("executable") or "").strip()
    if configured:
        return Path(configured)
    info = variant(app)
    return _local_programs() / info["product"] / info["image"]


def trae_state_db(app: str = "trae_cn", config: dict[str, Any] | None = None) -> Path:
    configured = str((config or {}).get("trae_state_db") or "").strip()
    if configured:
        return Path(configured)
    return _appdata() / variant(app)["user_data"] / "User" / "globalStorage" / "state.vscdb"


def trae_home(app: str = "trae_cn", config: dict[str, Any] | None = None) -> Path:
    configured = str((config or {}).get("trae_home") or "").strip()
    return Path(configured) if configured else _home() / variant(app)["home"]


def _decode(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")


def _read_items(db: Path) -> dict[str, str]:
    if not db.exists():
        raise FileNotFoundError(f"Trae 状态库不存在: {db}")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {str(k): _decode(v) for k, v in conn.execute("select key, value from ItemTable")}
    finally:
        conn.close()


def account_uid(items: dict[str, str]) -> str:
    """The logged-in account id used as a state key prefix."""
    found = set()
    for key in items:
        match = re.match(r"^(\d{10,})[:_]", key)
        if match:
            found.add(match.group(1))
    if len(found) != 1:
        # Multiple accounts: prefer the one that owns the model catalog.
        owners = {k.split(":")[0] for k in items
                  if k.endswith(":AI.agent.model.model_list_map")}
        if len(owners) == 1:
            return next(iter(owners))
        raise ValueError(f"无法确定 Trae 账号 uid（候选 {sorted(found)}）")
    return next(iter(found))


def backup_state_db(db: Path) -> Path:
    """Copy the state store (and its WAL siblings) before any write."""
    if not db.exists():
        raise FileNotFoundError(f"Trae 状态库不存在: {db}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = db.with_name(f"{db.name}.agentmesh-{stamp}.bak")
    if backup.exists():
        return backup
    # SQLite backup API includes committed WAL frames; a raw file copy can miss
    # the newest writes while Trae is running.
    source = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15)
    try:
        target = sqlite3.connect(str(backup))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return backup


def _write_items(db: Path, changes: dict[str, str], *, backup: bool = True) -> dict[str, Any]:
    backup_path = backup_state_db(db) if backup else ""
    conn = sqlite3.connect(str(db), timeout=15)
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        for key, value in changes.items():
            conn.execute(
                "INSERT INTO ItemTable(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        conn.commit()
    finally:
        conn.close()
    return {"db": str(db), "backup": str(backup_path) if backup_path else "",
            "keys_written": sorted(changes)}


def _json(value: str, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (json.JSONDecodeError, TypeError):
        return default


def model_rate(entry: dict[str, Any]) -> float | None:
    """Effective credit multiplier from the app's own catalog entry.

    Trae stores it in one of three places depending on the campaign:
    ``features.discount`` (member price), ``features.activity_discount``
    (off-peak) or ``features.consumption_rate`` (list price).
    """
    features = entry.get("features") if isinstance(entry.get("features"), dict) else {}
    candidates = [
        ((features.get("discount") or {}).get("data") or {}).get("consumption_rate"),
        (((features.get("activity_discount") or {}).get("data") or {})
         .get("current") or {}).get("consumption_rate"),
        ((features.get("consumption_rate") or {}).get("data") or {}).get("rate"),
        ((features.get("activity_discount") or {}).get("data") or {})
        .get("before_consumption_rate"),
    ]
    for value in candidates:
        try:
            if value is not None and float(value) > 0:
                return round(float(value), 4)
        except (TypeError, ValueError):
            continue
    return None


def list_trae_models(app: str = "trae_cn",
                     config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Read the real, selectable model catalog from the app's own state store."""
    db = trae_state_db(app, config)
    items = _read_items(db)
    uid = account_uid(items)
    catalog = _json(items.get(f"{uid}:AI.agent.model.model_list_map", ""), {})
    if not isinstance(catalog, dict):
        return []
    out: list[dict[str, Any]] = []
    for label, rows in catalog.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name or row.get("selectable") is False or row.get("status") is False:
                continue
            out.append({
                "id": name,
                "name": name,
                "display_name": str(row.get("display_name") or name),
                "agent_label": label,
                "config_source": int(row.get("config_source") or 1),
                "model_id": f"{label}_{int(row.get('config_source') or 1)}__{name}",
                "rate": model_rate(row),
                "fee_level": int(row.get("fee_model_level") or 0),
                "source": "state.vscdb:AI.agent.model.model_list_map",
                "evidence": str(db),
            })
    return out


def resolve_trae_model(model: str, *, app: str = "trae_cn",
                       config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Resolve a user-facing model name to a REAL catalog entry.

    Accepts the catalog id (``deepseek-v4.1-flash``), the display name
    (``DeepSeek-V4.1-Flash``) or an already-composed modelId.  Returns None for
    anything not present in the application's own catalog -- never a guess.
    """
    wanted = str(model or "").strip().lower()
    if not wanted:
        return None
    rows = list_trae_models(app, config)
    for row in rows:
        if wanted in (row["id"].lower(), row["display_name"].lower(),
                      row["model_id"].lower()):
            return row
    return None


def model_key(app: str, model: str, config: dict[str, Any] | None = None) -> str:
    row = resolve_trae_model(model, app=app, config=config)
    return row["model_id"] if row else ""


@dataclass(frozen=True)
class TraeState:
    app: str
    uid: str
    db: Path
    items: dict[str, str]

    def get(self, key: str, default: Any = None) -> Any:
        return _json(self.items.get(key, ""), default)

    def prefixed(self, key: str) -> str:
        return f"{self.uid}:{key}"

    def underscored(self, key: str) -> str:
        return f"{self.uid}_{key}"


def read_state(app: str = "trae_cn", config: dict[str, Any] | None = None) -> TraeState:
    db = trae_state_db(app, config)
    items = _read_items(db)
    return TraeState(app=app, uid=account_uid(items), db=db, items=items)


def set_trae_model(*, model: str, app: str = "trae_cn",
                   config: dict[str, Any] | None = None,
                   session_id: str = "", cdp_port: int = 0) -> InterfaceOutcome:
    """Set the model: composer menu first (authoritative), store as persistence.

    Trae's SOLO preference is server-synced, so the store write alone does not
    change the runtime model.  When the renderer is reachable we click the
    app's own menu entry and verify the visible name; the store write then keeps
    the choice for the next launch.
    """
    if cdp_port:
        ui = select_trae_model_via_cdp(model, port=int(cdp_port), app=app, config=config)
        if not ui.ok:
            return ui
        store = _write_trae_model_store(model=model, app=app, config=config,
                                        session_id=session_id)
        return InterfaceOutcome(
            True, "cdp-input+state", ui.expected,
            {**ui.actual, "store_ok": store.ok, "store_method": store.method},
            evidence={**ui.evidence, "store": store.evidence},
            reason="" if store.ok else f"界面已切换，但状态库写入失败: {store.reason}",
        )
    return _write_trae_model_store(model=model, app=app, config=config,
                                   session_id=session_id)


def _write_trae_model_store(*, model: str, app: str = "trae_cn",
                            config: dict[str, Any] | None = None,
                            session_id: str = "") -> InterfaceOutcome:
    """Persist the selection in Trae's own applicationStorage keys."""
    try:
        state = read_state(app, config)
    except Exception as exc:
        return InterfaceOutcome(False, "state_write", {"model": model}, {},
                                reason=f"读取 Trae 状态库失败: {exc}",
                                error_code="INTERFACE_WRITE_FAILED")
    row = resolve_trae_model(model, app=app, config=config)
    if not row:
        available = sorted({r["id"] for r in list_trae_models(app, config)})
        return InterfaceOutcome(
            False, "state_write", {"model": model}, {"available": available[:40]},
            reason=f"Trae 真实模型清单中没有 {model}",
            error_code="MODEL_UNAVAILABLE",
        )
    key = row["model_id"]
    label = row["agent_label"]
    selection = {"modelId": key, "mode": MODEL_MODE}
    recent = state.get(state.prefixed("AI.agent.model.recent_user_selection_by_agent_label"), {})
    if not isinstance(recent, dict):
        recent = {}
    recent[label] = selection
    session_map = state.get(state.prefixed("AI.agent.model.session_selected_model"), {})
    if not isinstance(session_map, dict):
        session_map = {}
    sessions = [session_id] if session_id else _active_session_ids(state)
    for sid in sessions:
        entry = session_map.get(sid) if isinstance(session_map.get(sid), dict) else {}
        entry[label] = selection
        session_map[sid] = entry
    full = _json(state.items.get(state.underscored("AI.agent.model.selected_model"), ""), {})
    if not isinstance(full, dict):
        full = {}
    full.update({"name": row["name"], "display_name": row["display_name"],
                 "config_source": row["config_source"]})
    changes = {
        state.prefixed("AI.agent.model.recent_user_selection_by_agent_label"):
            json.dumps(recent, ensure_ascii=False),
        state.prefixed("AI.agent.model.session_selected_model"):
            json.dumps(session_map, ensure_ascii=False),
        state.underscored("AI.agent.model.selected_model"):
            json.dumps(full, ensure_ascii=False),
    }
    try:
        written = _write_items(state.db, changes)
    except Exception as exc:
        return InterfaceOutcome(False, "state_write", {"model": key}, {},
                                reason=f"写入 Trae 状态库失败: {exc}",
                                error_code="INTERFACE_WRITE_FAILED")
    return InterfaceOutcome(
        True, "state_write", {"model": key, "agent_label": label},
        {"model": key, "sessions": sessions[:10]},
        evidence={**written, "source": "state.vscdb ItemTable",
                  "catalog_entry": {k: row[k] for k in ("id", "display_name", "model_id")}},
    )


def _active_session_ids(state: TraeState) -> list[str]:
    modes = state.get(f"solo-lite-mode-state-map-{state.uid}", {})
    out: list[str] = []
    if isinstance(modes, dict):
        for mode in DEV_MODES:
            entry = modes.get(mode)
            if isinstance(entry, dict) and isinstance(entry.get("session"), dict):
                sid = str(entry["session"].get("sessionId") or "").strip()
                if sid and sid not in out:
                    out.append(sid)
    return out


def set_trae_workspace(*, workspace: str, app: str = "trae_cn",
                       dev_mode: str = "code",
                       config: dict[str, Any] | None = None) -> InterfaceOutcome:
    """Bind Trae's solo-lite session to a real folder, using its own state keys."""
    root = Path(workspace).expanduser().resolve()
    ensure_dir(root)
    if dev_mode not in DEV_MODES:
        return InterfaceOutcome(False, "state_write", {"dev_mode": dev_mode}, {},
                                reason=f"未知开发模式 {dev_mode}，合法值 {list(DEV_MODES)}",
                                error_code="GUI_MODE_MISMATCH")
    try:
        state = read_state(app, config)
    except Exception as exc:
        return InterfaceOutcome(False, "state_write", {"workspace": str(root)}, {},
                                reason=f"读取 Trae 状态库失败: {exc}",
                                error_code="INTERFACE_WRITE_FAILED")
    path = str(root)
    path_lower = path.lower()
    project_id = hashlib.sha256(path_lower.encode("utf-8")).hexdigest()[:24]
    folders = state.get("solo-lite.local-project-folders", {})
    if not isinstance(folders, dict):
        folders = {}
    folders[project_id] = path_lower
    cache = state.get("solo-lite.project-workspace-cache", {})
    if not isinstance(cache, dict):
        cache = {}
    cache[project_id] = {"identity": path_lower, "available": True,
                         "folders": [path_lower], "name": root.name}
    recent = state.get(state.prefixed("workspace-selector.recent.v1"), [])
    if not isinstance(recent, list):
        recent = []
    recent = [{"kind": "local", "key": project_id, "title": path_lower}] + [
        item for item in recent if not (isinstance(item, dict)
                                        and str(item.get("title", "")).lower() == path_lower)
    ]
    mode_key = f"solo-lite-mode-state-map-{state.uid}"
    modes = state.get(mode_key, {})
    if not isinstance(modes, dict):
        modes = {}
    for mode in DEV_MODES:
        if not isinstance(modes.get(mode), dict):
            modes[mode] = {"session": None, "showFileTree": False}
    active = None
    for mode in DEV_MODES:
        entry = modes.get(mode) or {}
        if isinstance(entry.get("session"), dict) and entry["session"].get("sessionId"):
            active = entry["session"]
            modes[mode] = {"session": None, "showFileTree": False}
            break
    if active is None:
        # No existing session: do not invent a session id.  The desktop will
        # create one when it opens the folder; workspace binding is still
        # recorded in the project caches above.
        active = None
    else:
        active["mode"] = dev_mode
        source = active.get("source") if isinstance(active.get("source"), dict) else {}
        source["type"] = "local"
        source["local_folder"] = path_lower
        source["remote_folder"] = path_lower
        source["repo_url"] = ""
        active["source"] = source
        active["local_folder"] = path_lower
        active["mainFolder"] = path_lower
        active["workspacePath"] = path_lower
        active["localProjectId"] = project_id
        active["isDefaultProject"] = False
        modes[dev_mode] = {"session": active, "showFileTree": False}
    changes = {
        "solo-lite.local-project-folders": json.dumps(folders, ensure_ascii=False),
        "solo-lite.project-workspace-cache": json.dumps(cache, ensure_ascii=False),
        state.prefixed("workspace-selector.recent.v1"): json.dumps(recent[:20], ensure_ascii=False),
        mode_key: json.dumps(modes, ensure_ascii=False),
    }
    if active:
        changes[f"solo-lite:route:{state.uid}"] = f"/session/{active['sessionId']}?env=local"
    try:
        written = _write_items(state.db, changes)
    except Exception as exc:
        return InterfaceOutcome(False, "state_write", {"workspace": path}, {},
                                reason=f"写入 Trae 工作区失败: {exc}",
                                error_code="INTERFACE_WRITE_FAILED")
    return InterfaceOutcome(
        True, "state_write", {"workspace": path, "dev_mode": dev_mode},
        {"workspace": path, "dev_mode": dev_mode,
         "session_id": str(active.get("sessionId", "")) if active else "",
         "local_project_id": project_id},
        evidence={**written, "source": "solo-lite state keys + mode-state-map",
                  "folder_uri": root.as_uri()},
    )


def _page_transport(port: int, timeout: float = 15.0):
    from ..gui.cdp import WebSocketTransport, _pick_page, _targets
    page = _pick_page(_targets(port))
    if not page:
        return None, None
    return page, WebSocketTransport(page["webSocketDebuggerUrl"],
                                    origin=f"http://127.0.0.1:{int(port)}", timeout=timeout)


def _click_xy(transport, x: float, y: float) -> None:
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        transport.call("Input.dispatchMouseEvent", {
            "type": kind, "x": float(x), "y": float(y), "button": "left",
            "buttons": 1 if kind != "mouseReleased" else 0, "clickCount": 1,
        }, timeout=10)
        time.sleep(0.12)


def open_trae_model_menu(port: int = CDP_DEFAULT_PORT) -> dict[str, Any]:
    """Open the composer model menu with page-level mouse events.

    ``Input.dispatchMouseEvent`` is delivered to the renderer's own event loop;
    it does not move the OS cursor or synthesise a Win32 input event.  The
    composer component ignores synthetic ``click``/``PointerEvent`` objects,
    which is why the DOM-only path cannot open this menu.
    """
    page, transport = _page_transport(port)
    if not transport:
        return {"ok": False, "reason": "NO_PAGE"}
    try:
        from ..gui.cdp import _evaluate
        rect = _evaluate(transport, (
            "(() => { const el=document.querySelector('.model-select-area');"
            " if(!el) return null; const r=el.getBoundingClientRect();"
            " return {x:r.x+r.width/2,y:r.y+r.height/2}; })()"
        ), 10)
        if not rect:
            return {"ok": False, "reason": "NO_MODEL_SELECTOR"}
        _click_xy(transport, rect["x"], rect["y"])
        time.sleep(1.2)
        body = str(_evaluate(transport,
                             "(() => (document.body.innerText||''))()", 10) or "")
        return {"ok": True, "x": rect["x"], "y": rect["y"],
                "menu_visible": "内置模型" in body or "自定义模型" in body}
    finally:
        transport.close()


def list_trae_model_items(port: int = CDP_DEFAULT_PORT,
                          limit: int = 60) -> list[dict[str, Any]]:
    """Visible model rows in the open menu (text + clickable centre)."""
    page, transport = _page_transport(port)
    if not transport:
        return []
    try:
        from ..gui.cdp import _evaluate
        rows = _evaluate(transport, """
          (() => {
            const out = [];
            const seen = new Set();
            document.querySelectorAll('body *').forEach((el) => {
              const text = (el.innerText || '').trim();
              if (!text || text.length > 60) return;
              const first = text.split('\\n')[0].trim();
              if (!/^(Auto Mode|Seed|GLM|DeepSeek|Kimi|MiMo|MiniMax|Step|Qwen|Doubao)/i.test(first)) return;
              const r = el.getBoundingClientRect();
              if (r.width <= 0 || r.height <= 0) return;
              const key = first + '@' + Math.round(r.y);
              if (seen.has(key)) return;
              seen.add(key);
              out.push({text: first, full: text.slice(0, 120),
                        x: r.x + r.width / 2, y: r.y + r.height / 2,
                        w: r.width, h: r.height});
            });
            return out;
          })()
        """, 12) or []
        # Prefer the smallest clickable row per label (the leaf node).
        best: dict[str, dict[str, Any]] = {}
        for row in rows:
            label = str(row.get("text") or "")
            area = float(row.get("w") or 0) * float(row.get("h") or 0)
            if label not in best or area < float(best[label].get("w", 0)) * float(best[label].get("h", 1)):
                best[label] = row
        return sorted(best.values(), key=lambda item: item["y"])[:limit]
    finally:
        transport.close()


def select_trae_model_via_cdp(model: str, *, port: int = CDP_DEFAULT_PORT,
                              app: str = "trae_cn",
                              config: dict[str, Any] | None = None) -> InterfaceOutcome:
    """Choose a model in Trae's own composer menu, then verify the visible name."""
    resolved = resolve_trae_model(model, app=app, config=config)
    if not resolved:
        return InterfaceOutcome(False, "cdp-input", {"model": model}, {},
                                reason=f"Trae 真实模型清单中没有 {model}",
                                error_code="MODEL_UNAVAILABLE")
    display = resolved["display_name"]
    opened: dict[str, Any] = {}
    for attempt in range(10):
        opened = open_trae_model_menu(port)
        if opened.get("ok"):
            break
        time.sleep(1.5)
    if not opened.get("ok"):
        return InterfaceOutcome(False, "cdp-input", {"model": display}, opened,
                                reason=f"无法打开模型菜单: {opened.get('reason')}",
                                error_code="MODEL_SWITCH_UNSUPPORTED")
    items = list_trae_model_items(port)
    target = next((item for item in items if item["text"].lower() == display.lower()), None)
    if not target:
        return InterfaceOutcome(
            False, "cdp-input", {"model": display}, {"items": [i["text"] for i in items]},
            reason=f"菜单中没有 {display}",
            error_code="MODEL_UNAVAILABLE",
        )
    page, transport = _page_transport(port)
    if not transport:
        return InterfaceOutcome(False, "cdp-input", {"model": display}, {},
                                reason="CDP page 不可用", error_code="GUI_SEND_FAILED")
    try:
        _click_xy(transport, target["x"], target["y"])
        time.sleep(1.0)
    finally:
        transport.close()
    visible = trae_visible_model(port)
    ok = visible.lower() == display.lower()
    return InterfaceOutcome(
        ok, "cdp-input", {"model": display},
        {"visible_model": visible, "clicked": target["text"]},
        evidence={"x": target["x"], "y": target["y"],
                  "source": "Input.dispatchMouseEvent (renderer only, no OS input)"},
        reason="" if ok else f"点击后输入框显示 {visible or '(空)'}，期望 {display}",
        error_code="" if ok else "MODEL_MISMATCH",
    )


def trae_visible_model(port: int = CDP_DEFAULT_PORT) -> str:
    """Read the model name the composer actually displays (CDP DOM)."""
    from ..gui.cdp import WebSocketTransport, _evaluate, _pick_page, _targets
    page = _pick_page(_targets(port))
    if not page:
        return ""
    transport = WebSocketTransport(page["webSocketDebuggerUrl"],
                                   origin=f"http://127.0.0.1:{int(port)}", timeout=8)
    try:
        value = _evaluate(transport, (
            "(() => { const el = document.querySelector('.model-select-area-model'); "
            "return el ? (el.innerText || '').trim() : ''; })()"
        ), 8)
        return str(value or "").strip()
    except Exception:
        return ""
    finally:
        transport.close()


def verify_trae_session(*, workspace: str, model: str, dev_mode: str = "code",
                        app: str = "trae_cn",
                        config: dict[str, Any] | None = None,
                        cdp_port: int = 0) -> InterfaceOutcome:
    """Read back the workspace / model / mode from the app's own state store."""
    expected = {"workspace": str(Path(workspace).expanduser().resolve()).lower(),
                "model": model_key(app, model, config), "dev_mode": dev_mode}
    try:
        state = read_state(app, config)
    except Exception as exc:
        return InterfaceOutcome(False, "state_read", expected, {},
                                reason=f"读取 Trae 状态库失败: {exc}",
                                error_code="WORKSPACE_MISMATCH")
    recent = state.get(state.prefixed("AI.agent.model.recent_user_selection_by_agent_label"), {})
    selection = recent.get(AGENT_LABEL, {}) if isinstance(recent, dict) else {}
    actual_model = str(selection.get("modelId", "")) if isinstance(selection, dict) else ""
    modes = state.get(f"solo-lite-mode-state-map-{state.uid}", {})
    active_mode = ""
    actual_workspace = ""
    if isinstance(modes, dict):
        for mode in DEV_MODES:
            entry = modes.get(mode)
            session = entry.get("session") if isinstance(entry, dict) else None
            if isinstance(session, dict) and session.get("sessionId"):
                active_mode = mode
                actual_workspace = str(session.get("workspacePath")
                                       or session.get("mainFolder") or "").lower()
                break
    folders = state.get("solo-lite.project-workspace-cache", {})
    folder_known = any(
        isinstance(item, dict) and str(item.get("identity", "")).lower() == expected["workspace"]
        for item in folders.values()
    ) if isinstance(folders, dict) else False
    actual = {"workspace": actual_workspace, "model": actual_model,
              "dev_mode": active_mode, "folder_registered": folder_known}
    evidence = {"source": str(state.db), "uid": state.uid,
                "keys": ["AI.agent.model.recent_user_selection_by_agent_label",
                         "solo-lite-mode-state-map", "solo-lite.project-workspace-cache"]}
    resolved = resolve_trae_model(model, app=app, config=config)
    if cdp_port:
        # The store can say one thing while the composer renders another (Trae
        # keeps a server-synced preference).  Only the visible name proves the
        # runtime model actually switched.
        visible = trae_visible_model(int(cdp_port))
        expected_display = resolved["display_name"] if resolved else model
        actual["visible_model"] = visible
        evidence["visible_model"] = visible
        if not visible or visible.lower() != expected_display.lower():
            return InterfaceOutcome(
                False, "cdp+state_read", {**expected, "visible_model": expected_display},
                actual, evidence=evidence,
                reason=f"输入框显示模型为 {visible or '(空)'}，期望 {expected_display}",
                error_code="MODEL_MISMATCH",
            )
    code, reason = "", ""
    if not actual_model or actual_model != expected["model"]:
        code = "GUI_MODEL_SWITCH_FAILED"
        reason = f"期望模型 {expected['model']}，实际 {actual_model or '(无)'}"
    elif active_mode and active_mode != expected["dev_mode"]:
        code = "GUI_MODE_MISMATCH"
        reason = f"期望开发模式 {expected['dev_mode']}，实际 {active_mode}"
    elif actual_workspace and actual_workspace != expected["workspace"]:
        code = "WORKSPACE_MISMATCH"
        reason = f"期望工作区 {expected['workspace']}，实际 {actual_workspace}"
    elif not folder_known and not actual_workspace:
        code = "WORKSPACE_MISMATCH"
        reason = "Trae 项目缓存中没有该工作区，且当前会话未绑定工作区"
    return InterfaceOutcome(not code, "state_read", expected, actual,
                            evidence=evidence, reason=reason, error_code=code)


def build_trae_chat_uri(*, app: str, prompt: str, mode: str = "solo") -> str:
    """Verified deeplink that PREFILLS the chat composer (does not send)."""
    from urllib.parse import quote
    info = variant(app)
    return (f"{info['protocol']}://GitHub.copilot-chat?"
            f"agent={quote(mode, safe='')}&prompt={quote(str(prompt), safe='')}")


def clean_launch_env() -> dict[str, str]:
    """WorkBuddy sets ELECTRON_RUN_AS_NODE=1; Electron apps must not inherit it."""
    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env.pop("NODE_OPTIONS", None)
    return env


def _local_urlopen(url: str, *, timeout: float = 2.0):
    """Loopback probe that ignores HTTP_PROXY (the sandbox proxy returns 502)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(url, timeout=timeout)


def _cdp_alive(port: int) -> bool:
    try:
        with _local_urlopen(f"http://127.0.0.1:{int(port)}/json/version", timeout=2) as r:
            return bool(json.loads(r.read().decode("utf-8", "replace")).get("Browser"))
    except Exception:
        return False


def _process_running(image: str) -> bool:
    try:
        proc = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
                              capture_output=True, text=True, timeout=20,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return False
    return image.lower() in (proc.stdout or "").lower()


def ensure_trae_cdp(*, app: str = "trae_cn", port: int = CDP_DEFAULT_PORT,
                    workspace: str = "", executable: str = "",
                    config: dict[str, Any] | None = None,
                    allow_restart: bool | None = None,
                    timeout_seconds: float = 45.0,
                    force_restart: bool = False,
                    before_launch: Any = None) -> InterfaceOutcome:
    """Ensure a Trae renderer exposes DevTools without any OS input.

    A restart is only performed when the caller explicitly allows it (env
    ``AGENTMESH_ALLOW_APP_RESTART=1`` or ``allow_restart=True``); the state DB
    is backed up first.  The user's running editor is never killed implicitly.
    """
    port = int(port)
    if _cdp_alive(port) and not force_restart:
        return InterfaceOutcome(True, "cdp", {"port": port, "reused": True},
                                {"source": f"http://127.0.0.1:{port}/json/version"},
                                evidence={"port": port, "reused": True})
    if allow_restart is None:
        allow_restart = str(os.environ.get("AGENTMESH_ALLOW_APP_RESTART") or "").strip().lower() \
            in ("1", "true", "yes", "on")
    if not allow_restart:
        return InterfaceOutcome(
            False, "cdp", {"port": port}, {},
            reason="CDP 未就绪；未获授权重启 Trae（设置 AGENTMESH_ALLOW_APP_RESTART=1）",
            error_code="GUI_SEND_FAILED",
        )
    exe = Path(executable) if executable else trae_executable(app, config)
    if not exe.exists():
        return InterfaceOutcome(False, "cdp", {"port": port}, {},
                                reason=f"Trae 可执行文件不存在: {exe}",
                                error_code="GUI_DRIVER_ERROR")
    image = variant(app)["image"]
    backup = ""
    try:
        db = trae_state_db(app, config)
        if db.exists():
            backup = str(backup_state_db(db))
    except Exception as exc:
        return InterfaceOutcome(False, "cdp", {"port": port}, {},
                                reason=f"备份 Trae 状态库失败: {exc}",
                                error_code="INTERFACE_WRITE_FAILED")
    if _process_running(image):
        subprocess.run(["taskkill", "/F", "/IM", image, "/T"],
                       capture_output=True, timeout=30, check=False)
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and _process_running(image):
            time.sleep(0.5)
    # The desktop restores workspace/model from its own store at startup, so
    # the caller may write the store here, after the old process is gone and
    # before the new one reads it.
    if callable(before_launch):
        before_launch()
    args = [str(exe), f"--remote-debugging-port={port}", "--remote-allow-origins=*",
            "--new-window"]
    if workspace:
        args.append(f"--folder-uri={Path(workspace).resolve().as_uri()}")
    # DETACHED_PROCESS keeps the editor alive after the caller's console exits;
    # without it Windows tears the child down with the parent shell.
    subprocess.Popen(args, close_fds=True, env=clean_launch_env(),
                     creationflags=0x00000008)
    deadline = time.time() + float(timeout_seconds)
    while time.time() < deadline:
        if _cdp_alive(port):
            # The DevTools endpoint can answer before the workbench page exists;
            # callers need a page target to evaluate the composer at all.
            try:
                from ..gui.cdp import _pick_page, _targets
                if not _pick_page(_targets(port)):
                    time.sleep(1.0)
                    continue
            except Exception:
                time.sleep(1.0)
                continue
            return InterfaceOutcome(
                True, "cdp", {"port": port, "reused": False, "workspace": workspace},
                {"source": f"http://127.0.0.1:{port}/json/version"},
                evidence={"port": port, "reused": False, "executable": str(exe),
                          "backup": backup, "folder_uri": args[-1] if workspace else ""},
            )
        time.sleep(1.0)
    return InterfaceOutcome(False, "cdp", {"port": port}, {},
                            reason=f"Trae CDP 端口 {port} 在 {timeout_seconds}s 内未就绪",
                            error_code="GUI_SEND_FAILED",
                            )


TRAE_SUBMIT_JS = r"""(() => {
  const selectors = [
    '.icube-chat-input-action-btn-send',
    '.chat-input-v2-send-button',
    '[data-testid="send-button"]',
  ];
  for (const selector of selectors) {
    const btn = document.querySelector(selector);
    if (!btn) continue;
    const disabled = btn.disabled || btn.getAttribute('aria-disabled') === 'true'
      || /disabled/i.test(String(btn.className || ''));
    if (disabled) return {ok: false, reason: 'SEND_DISABLED', selector};
    btn.click();
    return {ok: true, method: 'class-click', selector};
  }
  const composer = document.querySelector('[contenteditable="true"]');
  const root = composer && composer.closest ? composer.closest('form') : null;
  if (root) { root.requestSubmit ? root.requestSubmit() : root.submit();
    return {ok: true, method: 'form.requestSubmit'}; }
  return {ok: false, reason: 'NO_SEND_BUTTON'};
})()"""


def send_trae_prompt(text: str, *, port: int = CDP_DEFAULT_PORT,
                     timeout: float = 15.0,
                     submit_retries: int = 20) -> dict[str, Any]:
    """Insert the prompt and click Trae's own send control through CDP DOM.

    Trae's send affordance is an icon-only ``div`` with class
    ``icube-chat-input-action-btn-send`` (source:
    ``workbench.desktop.main.js:3777``) and therefore has no aria-label for the
    generic submit helper to match.  We insert via the shared helper and then
    click the verified class selector.
    """
    import time as _time

    from ..gui.cdp import WebSocketTransport, _evaluate, _pick_page, _targets, send_prompt_via_cdp
    inserted = send_prompt_via_cdp(text, port=port, timeout=timeout, submit=False)
    page = _pick_page(_targets(port))
    if not page:
        from ..gui.base import GuiAutomationError
        raise GuiAutomationError("GUI_SEND_FAILED", "CDP 没有可用的 page 目标")
    transport = WebSocketTransport(page["webSocketDebuggerUrl"],
                                   origin=f"http://127.0.0.1:{int(port)}", timeout=timeout)
    try:
        last: dict[str, Any] = {}
        for attempt in range(max(1, int(submit_retries))):
            _time.sleep(0.4 if attempt == 0 else 0.6)
            last = _evaluate(transport, TRAE_SUBMIT_JS, timeout) or {}
            if last.get("ok"):
                return {"insert": inserted, "submit": {**last, "attempts": attempt + 1}}
            if last.get("reason") not in ("SEND_DISABLED", "NO_SEND_BUTTON"):
                break
        from ..gui.base import GuiAutomationError
        raise GuiAutomationError("GUI_SEND_FAILED",
                                 f"Trae 发送按钮点击失败: {last}")
    finally:
        transport.close()


def trae_conversation_text(*, port: int = CDP_DEFAULT_PORT,
                           limit: int = 12000) -> dict[str, Any]:
    """Read the visible conversation text through CDP (no private DB access)."""
    from ..gui.cdp import WebSocketTransport, _evaluate, _pick_page, _targets
    targets = _targets(port)
    page = _pick_page(targets)
    if not page:
        return {"ok": False, "reason": "NO_PAGE"}
    transport = WebSocketTransport(page["webSocketDebuggerUrl"],
                                   origin=f"http://127.0.0.1:{int(port)}", timeout=10)
    try:
        value = _evaluate(transport, (
            "(() => { const t = (document.body && document.body.innerText) || ''; "
            f"return {{ok: true, text: t.slice(-{int(limit)}), chars: t.length}}; }})()"
        ), 10)
        return value or {"ok": False, "reason": "EMPTY"}
    finally:
        transport.close()


def probe(app: str = "trae_cn", config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read-only evidence probe used by the 11-agent surface matrix."""
    exe = trae_executable(app, config)
    db = trae_state_db(app, config)
    out: dict[str, Any] = {
        "app": app,
        "product": variant(app)["product"],
        "executable": str(exe),
        "executable_exists": exe.exists(),
        "state_db": str(db),
        "state_db_exists": db.exists(),
        "state_db_plain_sqlite": db.exists() and db.open("rb").read(16) == b"SQLite format 3\x00",
        "url_protocol": variant(app)["protocol"],
        "cli": ["--folder-uri", "--new-window", "--user-data-dir", "chat -m/--mode"],
        "chat_deeplink": "GitHub.copilot-chat?agent=<mode>&prompt=<text> (prefill only)",
        "cdp": "--remote-debugging-port + clean ELECTRON_RUN_AS_NODE",
        "models": [],
        "uid": "",
        "dev_modes": list(DEV_MODES),
    }
    if not db.exists():
        return out
    try:
        state = read_state(app, config)
    except Exception as exc:
        out["error"] = str(exc)
        return out
    out["uid"] = state.uid
    models = list_trae_models(app, config)
    out["model_count"] = len(models)
    out["models"] = [m["id"] for m in models
                     if m["agent_label"] == AGENT_LABEL][:60]
    out["selected_model"] = state.get(
        state.prefixed("AI.agent.model.recent_user_selection_by_agent_label"), {})
    out["route"] = state.items.get(f"solo-lite:route:{state.uid}", "")
    out["workspace_cache_count"] = len(
        state.get("solo-lite.project-workspace-cache", {}) or {})
    return out


__all__ = [
    "APP_VARIANTS", "CDP_DEFAULT_PORT", "DEV_MODES", "AGENT_LABEL",
    "backup_state_db", "read_state", "account_uid", "list_trae_models",
    "resolve_trae_model", "model_key", "set_trae_model", "set_trae_workspace",
    "verify_trae_session", "build_trae_chat_uri", "clean_launch_env",
    "ensure_trae_cdp", "send_trae_prompt", "trae_conversation_text",
    "trae_visible_model", "open_trae_model_menu", "list_trae_model_items",
    "select_trae_model_via_cdp", "trae_executable", "trae_state_db", "trae_home",
    "variant", "probe",
]
