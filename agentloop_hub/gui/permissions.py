"""GUI permission interfaces: full-access switches and dialog auto-accept.

Everything here uses the agent's own renderer (CDP `Runtime.evaluate` +
DOM `.click()`).  No OS mouse/keyboard/clipboard events are synthesised.

Verified on this machine (2026-11-04):

* Trae CN 1.107.1: composer button ``button[data-permission-mode-trigger="true"]``
  opens a menu whose item text is ``完全访问`` / ``手动审批`` / ``自动审批``.
* WorkBuddy 5.9.3: composer control ``.cr-permission-setting`` shows
  ``允许完全访问`` or ``默认权限``; its popover contains a switch labelled
  ``允许完全访问``.  File-safety prompts expose the observed buttons
  ``允许读取`` / ``本次会话内始终允许`` / ``拒绝`` (and analogous write/command
  texts).
* Qoder CN: session row ``chat_sessions.permission_mode`` uses
  ``bypassPermissions`` for the UI's ``完全访问`` mode; permission cards expose
  ``允许`` / ``允许一次`` / ``本次任务允许`` / ``始终允许``.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .cdp import WebSocketTransport, _evaluate, _pick_page, _targets

# Labels are ordered: the strongest "always/session" grant first, then a
# one-shot grant.  Short generic labels are only clicked inside a permission
# container to avoid touching unrelated UI.
APP_LABELS: dict[str, list[str]] = {
    "workbuddy": [
        "本次会话内始终允许",
        "始终允许",
        "允许读取",
        "允许执行",
        "允许写入",
        "允许本次会话",
        "允许一次",
    ],
    "trae_cn": [
        "始终允许",
        "允许本次会话",
        "允许一次",
        "允许",
    ],
    "trae": [
        "始终允许",
        "允许本次会话",
        "允许一次",
        "允许",
    ],
    "qoder_cn": [
        "始终允许",
        "本次任务允许",
        "允许一次",
        "允许",
    ],
}

ALWAYS_LABELS = {
    "本次会话内始终允许", "始终允许", "本次任务允许", "允许本次会话",
}


def _script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _transport(port: int) -> WebSocketTransport:
    page = _pick_page(_targets(int(port)))
    if not page:
        raise RuntimeError(f"CDP 端口 {port} 没有可用的 page 目标")
    return WebSocketTransport(
        page["webSocketDebuggerUrl"], origin=f"http://127.0.0.1:{int(port)}",
        timeout=10,
    )


def _probe_and_click(port: int, labels: list[str], *,
                     transport: WebSocketTransport | None = None) -> dict[str, Any]:
    js = r"""(() => {
      const labels = %s;
      const always = %s;
      const vis = (el) => el && el.getClientRects().length > 0;
      const inDialog = (el) => el.closest(
        '[role="dialog"],[class*="permission"],[class*="Permission"],' +
        '[class*="confirm"],[class*="Confirm"],[class*="approval"],' +
        '[class*="Approval"],[class*="modal"],[class*="Modal"]'
      );
      const nodes = [...document.querySelectorAll(
        'button,[role="button"],[role="menuitem"],li,div'
      )].filter(vis);
      const matches = [];
      for (const el of nodes) {
        const text = (el.innerText || '').trim();
        if (!text) continue;
        for (const label of labels) {
          if (text === label || text.startsWith(label + '\n')) {
            if (el.querySelector('button,[role="button"]')) continue;
            matches.push({el, label, text});
            break;
          }
        }
      }
      // Prefer the strongest grant; then the one inside a permission dialog.
      matches.sort((a, b) => {
        const aa = always.includes(a.label) ? 1 : 0;
        const bb = always.includes(b.label) ? 1 : 0;
        return bb - aa;
      });
      for (const m of matches) {
        const dialog = inDialog(m.el);
        if (!dialog && m.label.length < 4) continue;
        const before = {label: m.label, text: m.text.slice(0, 200),
                        tag: m.el.tagName,
                        cls: String(m.el.className || '').slice(0, 160),
                        container: dialog ? String(dialog.className || '').slice(0, 160) : ''};
        try { m.el.click(); } catch (err) {
          m.el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
        }
        return {ok: true, clicked: before};
      }
      return {ok: false, reason: 'NO_MATCH', labels};
    })()""" % (_script(labels), _script(sorted(ALWAYS_LABELS)))
    own = transport is None
    tr = transport or _transport(port)
    try:
        return _evaluate(tr, js, 10) or {"ok": False, "reason": "EMPTY"}
    finally:
        if own:
            tr.close()


def accept_permission_once(app: str, port: int) -> dict[str, Any]:
    labels = APP_LABELS.get(str(app or "").lower())
    if not labels:
        return {"ok": False, "reason": "APP_NOT_SUPPORTED", "app": app}
    return _probe_and_click(int(port), labels)


class PermissionWatcher:
    """Background CDP polling while a GUI agent is working."""

    def __init__(self, *, app: str, port: int, log_path: str | Path | None = None,
                 interval: float = 1.0, clicker: Callable[[str, int], dict[str, Any]] | None = None):
        self.app = str(app or "").lower()
        self.port = int(port)
        self.log_path = Path(log_path) if log_path else None
        self.interval = float(interval)
        self.clicker = clicker or accept_permission_once
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.accepted: list[dict[str, Any]] = []
        self.errors: list[str] = []

    def start(self) -> "PermissionWatcher":
        if self.app not in APP_LABELS:
            return self
        self._thread = threading.Thread(target=self._run, name=f"perm-{self.app}",
                                        daemon=True)
        self._thread.start()
        self._log("permission_watcher_start", app=self.app, port=self.port)
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(2.0, self.interval * 2))
        self._log("permission_watcher_stop", app=self.app,
                  accepted=len(self.accepted), errors=len(self.errors))

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                result = self.clicker(self.app, self.port)
            except Exception as exc:  # transient CDP reloads are normal
                self.errors.append(str(exc)[:300])
                continue
            if result.get("ok") and result.get("clicked"):
                item = {"at": time.time(), **result["clicked"]}
                self.accepted.append(item)
                self._log("permission_auto_accept", app=self.app, **item)
                # A dialog may stack; keep polling.  Small pause avoids
                # double-clicking a button that is being replaced.
                self._stop.wait(0.6)

    def _log(self, event: str, **payload: Any) -> None:
        if not self.log_path:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                               "event": event, **payload}, ensure_ascii=False)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Full-access switches
# ---------------------------------------------------------------------------

def _click_trae_full_access_confirm(tr: WebSocketTransport) -> dict[str, Any]:
    js = r"""(() => {
      const vis = (el) => el && el.getClientRects().length > 0;
      const dialogs = [...document.querySelectorAll(
        '[role="dialog"],[class*="modal"],[class*="Modal"],[class*="confirm"],[class*="Confirm"],' +
        '[class*="dialog"],[class*="Dialog"],[class*="overlay"],[class*="Overlay"]'
      )].filter(vis);
      for (const dialog of dialogs) {
        const text = (dialog.innerText || '');
        if (!/启用完全访问|完全访问/.test(text)) continue;
        const buttons = [...dialog.querySelectorAll('button,[role="button"]')].filter(vis);
        for (const button of buttons) {
          const label = (button.innerText || '').trim();
          if (label === '启用' || label === '确认' || label === '确定') {
            button.click();
            return {ok: true, label, dialog: text.slice(0, 400)};
          }
        }
      }
      return {ok: false, reason: 'NO_CONFIRM'};
    })()"""
    return _evaluate(tr, js, 10) or {}


def ensure_trae_full_access(*, port: int, log_path: str | Path | None = None,
                            transport: WebSocketTransport | None = None) -> dict[str, Any]:
    """Set Trae's composer permission mode to 完全访问 via its own menu."""
    own = transport is None
    tr = transport or _transport(port)
    try:
        # A previously opened confirmation can survive a failed attempt.
        confirm = _click_trae_full_access_confirm(tr)
        if confirm.get("ok"):
            time.sleep(0.6)
        probe = _evaluate(tr, r"""(() => {
          const t = document.querySelector('button[data-permission-mode-trigger="true"]');
          return t ? {ok: true, mode: t.getAttribute('aria-label') || '',
                      text: (t.innerText || '').trim()} : {ok: false, reason: 'NO_TRIGGER'};
        })()""", 10) or {}
        if not probe.get("ok"):
            return {"ok": False, "reason": probe.get("reason", "NO_TRIGGER")}
        if probe.get("mode") == "完全访问":
            _log_interface(log_path, "trae_permission_mode", ok=True,
                           mode="完全访问", already=True)
            return {"ok": True, "mode": "完全访问", "already": True}
        _evaluate(tr, r"""(() => {
          const t = document.querySelector('button[data-permission-mode-trigger="true"]');
          if (t) t.click();
          return true;
        })()""", 10)
        time.sleep(0.5)
        clicked = _evaluate(tr, r"""(() => {
          const vis = (el) => el && el.getClientRects().length > 0;
          const items = [...document.querySelectorAll('[class*="permissionModeItem"]')]
            .filter(vis);
          for (const item of items) {
            const text = (item.innerText || '').trim();
            if (text === '完全访问' || text.startsWith('完全访问\n')) {
              const name = item.querySelector('[class*="permissionModeItemName"]') || item;
              name.click();
              return {ok: true, text: text.slice(0, 200),
                      cls: String(item.className || '').slice(0, 160)};
            }
          }
          return {ok: false, reason: 'NO_FULL_ACCESS_ITEM'};
        })()""", 10) or {}
        time.sleep(0.5)
        confirm = _click_trae_full_access_confirm(tr)
        if confirm.get("ok"):
            time.sleep(0.6)
        verify = _evaluate(tr, r"""(() => {
          const t = document.querySelector('button[data-permission-mode-trigger="true"]');
          return {mode: t ? (t.getAttribute('aria-label') || '') : ''};
        })()""", 10) or {}
        ok = str(verify.get("mode") or "") == "完全访问"
        _log_interface(log_path, "trae_permission_mode", ok=ok,
                       mode=verify.get("mode"), clicked=clicked, confirm=confirm)
        return {"ok": ok, "mode": verify.get("mode"), "clicked": clicked,
                "confirm": confirm}
    finally:
        if own:
            tr.close()


def ensure_workbuddy_full_access(*, port: int,
                                 log_path: str | Path | None = None,
                                 transport: WebSocketTransport | None = None) -> dict[str, Any]:
    """Turn on WorkBuddy's composer ``允许完全访问`` switch via CDP."""
    own = transport is None
    tr = transport or _transport(port)
    try:
        state = _evaluate(tr, r"""(() => {
          const vis = (el) => el && el.getClientRects().length > 0;
          const b = [...document.querySelectorAll('button')].find(
            (x) => vis(x) && String(x.className || '').includes('cr-permission-setting'));
          if (!b) return {ok: false, reason: 'NO_SETTING'};
          return {ok: true, text: (b.innerText || '').trim(),
                  full: (b.innerText || '').includes('允许完全访问')};
        })()""", 10) or {}
        if not state.get("ok"):
            return {"ok": False, "reason": state.get("reason", "NO_SETTING")}
        if state.get("full"):
            _log_interface(log_path, "workbuddy_permission_mode", ok=True,
                           mode="允许完全访问", already=True)
            return {"ok": True, "mode": "允许完全访问", "already": True}
        _evaluate(tr, r"""(() => {
          const vis = (el) => el && el.getClientRects().length > 0;
          const b = [...document.querySelectorAll('button')].find(
            (x) => vis(x) && String(x.className || '').includes('cr-permission-setting'));
          if (b) b.click();
          return true;
        })()""", 10)
        time.sleep(0.5)
        switch = _evaluate(tr, r"""(() => {
          const vis = (el) => el && el.getClientRects().length > 0;
          const sw = [...document.querySelectorAll('input[type="checkbox"]')].find(
            (x) => vis(x) && (x.getAttribute('aria-label') || '') === '允许完全访问');
          if (!sw) return {ok: false, reason: 'NO_SWITCH'};
          const before = !!sw.checked;
          if (!sw.checked) sw.click();
          return {ok: true, before, after: !!sw.checked};
        })()""", 10) or {}
        _evaluate(tr, r"""(() => {
          const vis = (el) => el && el.getClientRects().length > 0;
          const b = [...document.querySelectorAll('button')].find(
            (x) => vis(x) && String(x.className || '').includes('cr-permission-setting'));
          if (b) b.click();
          return true;
        })()""", 10)
        time.sleep(0.4)
        verify = _evaluate(tr, r"""(() => {
          const vis = (el) => el && el.getClientRects().length > 0;
          const b = [...document.querySelectorAll('button')].find(
            (x) => vis(x) && String(x.className || '').includes('cr-permission-setting'));
          return {text: b ? (b.innerText || '').trim() : ''};
        })()""", 10) or {}
        ok = "允许完全访问" in str(verify.get("text") or "")
        _log_interface(log_path, "workbuddy_permission_mode", ok=ok,
                       mode=verify.get("text"), switch=switch)
        return {"ok": ok, "mode": verify.get("text"), "switch": switch}
    finally:
        if own:
            tr.close()


def _log_interface(log_path: str | Path | None, event: str, **payload: Any) -> None:
    if not log_path:
        return
    try:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                                     "event": event, **payload},
                                    ensure_ascii=False) + "\n")
    except Exception:
        pass


__all__ = [
    "APP_LABELS", "ALWAYS_LABELS", "PermissionWatcher", "accept_permission_once",
    "ensure_trae_full_access", "ensure_workbuddy_full_access",
]
