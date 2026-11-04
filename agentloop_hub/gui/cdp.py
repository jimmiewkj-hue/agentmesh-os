"""CDP DOM channel for zero-input prompt submission.

The renderer of an Electron agent is Chromium.  When it is launched with
``--remote-debugging-port`` we can write the composer text and press the page's
own submit control through ``Runtime.evaluate``.  This module deliberately does
**not** use ``Input.dispatchKeyEvent``, ``Input.insertText`` or the OS
clipboard: those generate input events (or mutate OS state) and are forbidden by
the project's zero-input policy.

Verified call sequence::

    GET  /json/list                      -> page target + webSocketDebuggerUrl
    WS   Runtime.evaluate {expr: insert} -> DOM text insertion
    WS   Runtime.evaluate {expr: submit} -> click the composer's send button

Every step returns a structured record so the adapter can log which DOM method
was used (``execCommand.insertText`` / native value setter / button click /
``form.requestSubmit``).
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Protocol

from .base import GuiAutomationError


class CdpTransport(Protocol):
    def call(self, method: str, params: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        ...

    def close(self) -> None:
        ...


def _targets(port: int) -> list[dict[str, Any]]:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{int(port)}/json/list", timeout=5
    ) as response:
        return json.loads(response.read().decode("utf-8"))


def _pick_page(targets: list[dict[str, Any]], page_id: str = "") -> dict[str, Any] | None:
    pages = [item for item in targets if item.get("type") == "page"]
    if page_id:
        return next((item for item in pages if item.get("id") == page_id), None)
    preferred = next((item for item in pages if "renderer" in str(item.get("title")).lower()),
                     None)
    if preferred:
        return preferred
    return pages[0] if pages else None


class WebSocketTransport:
    """Thin request/response wrapper over ``websocket-client``."""

    def __init__(self, url: str, *, origin: str, timeout: float = 10.0):
        try:
            import websocket  # type: ignore
        except Exception as exc:  # pragma: no cover - dependency guard
            raise GuiAutomationError("GUI_SEND_FAILED",
                                     f"websocket-client 不可用: {exc}") from exc
        self._ws = websocket.create_connection(url, timeout=timeout, origin=origin)
        self._timeout = timeout
        self._next_id = 0

    def call(self, method: str, params: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        self._next_id += 1
        call_id = self._next_id
        self._ws.send(json.dumps({"id": call_id, "method": method, "params": params}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = json.loads(self._ws.recv())
            if message.get("id") == call_id:
                if message.get("error"):
                    raise GuiAutomationError(
                        "GUI_SEND_FAILED",
                        f"CDP {method} 失败: {message['error']}",
                    )
                return message
        raise GuiAutomationError("GUI_SEND_FAILED", f"CDP {method} 超时")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


def _evaluate(transport: CdpTransport, expression: str, timeout: float) -> Any:
    response = transport.call("Runtime.evaluate", {
        "expression": expression, "returnByValue": True, "awaitPromise": False,
    }, timeout=timeout)
    value = response.get("result", {}).get("result", {}).get("value")
    if value is None and response.get("result", {}).get("exceptionDetails"):
        raise GuiAutomationError(
            "GUI_SEND_FAILED",
            f"CDP 页面脚本异常: {response['result']['exceptionDetails']}",
        )
    return value


INSERT_JS = r"""(() => {
  const text = %s;
  const composer = document.querySelector('[contenteditable="true"]')
    || document.querySelector('[role="textbox"]')
    || document.querySelector('textarea');
  if (!composer) return {ok: false, reason: 'NO_COMPOSER'};
  composer.focus();
  let method = '';
  try {
    method = 'execCommand.insertText';
    const ok = document.execCommand('insertText', false, text);
    if (!ok) method = '';
  } catch (err) { method = ''; }
  if (!method) {
    if (composer.tagName === 'TEXTAREA' || composer.tagName === 'INPUT') {
      const proto = Object.getPrototypeOf(composer);
      const desc = Object.getOwnPropertyDescriptor(proto, 'value');
      if (desc && desc.set) desc.set.call(composer, text); else composer.value = text;
      method = 'native.value+input';
    } else {
      composer.textContent = text;
      method = 'textContent+input';
    }
    composer.dispatchEvent(new InputEvent('input', {
      bubbles: true, inputType: 'insertText', data: text,
    }));
  }
  return {
    ok: true, method, tag: composer.tagName,
    editable: !!composer.isContentEditable,
    chars: String(text).length,
  };
})()""" % json.dumps("__TEXT__")


SUBMIT_JS = r"""(() => {
  const composer = document.querySelector('[contenteditable="true"]')
    || document.querySelector('[role="textbox"]')
    || document.querySelector('textarea');
  const roots = [];
  let node = composer;
  for (let i = 0; i < 5 && node; i += 1) { roots.push(node); node = node.parentElement; }
  const seen = new Set();
  const candidates = [];
  const collect = (root) => {
    if (!root || !root.querySelectorAll) return;
    for (const el of root.querySelectorAll('button,[role="button"]')) {
      if (!seen.has(el)) { seen.add(el); candidates.push(el); }
    }
  };
  roots.forEach(collect);
  if (!candidates.length) collect(document);
  const bad = /(stop|停止|cancel|取消|interrupt|中断)/i;
  const good = /(send|发送|submit|提交|start|开始|run|运行|生成|create)/i;
  for (const btn of candidates) {
    const label = [
      btn.getAttribute('aria-label'), btn.getAttribute('title'),
      btn.getAttribute('data-testid'), btn.textContent,
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
    if (!label || bad.test(label)) continue;
    if (good.test(label) && !btn.disabled) {
      btn.click();
      return {ok: true, method: 'button-click', label: label.slice(0, 80)};
    }
    if (btn.type === 'submit' && !btn.disabled) {
      btn.click();
      return {ok: true, method: 'submit-button', label: label.slice(0, 80)};
    }
  }
  const form = composer && composer.closest ? composer.closest('form') : null;
  if (form) {
    if (form.requestSubmit) form.requestSubmit(); else form.submit();
    return {ok: true, method: 'form.requestSubmit'};
  }
  return {ok: false, reason: 'NO_SUBMIT'};
})()"""


def wait_for_cdp(port: int = 9222, *, timeout: float = 60.0,
                 poll: float = 1.0) -> dict[str, Any]:
    """Wait until the Electron renderer exposes its DevTools endpoint."""
    deadline = time.time() + float(timeout)
    last = ""
    while time.time() < deadline:
        try:
            targets = _targets(port)
            page = _pick_page(targets)
            if page:
                return {"ok": True, "page": page, "port": int(port),
                        "targets": len(targets)}
            last = "no page target"
        except Exception as exc:
            last = str(exc)
        time.sleep(poll)
    raise GuiAutomationError(
        "GUI_SEND_FAILED",
        f"CDP 端口 {int(port)} 在 {int(timeout)}s 内不可用: {last}",
    )


def send_prompt_via_cdp(text: str, *, port: int = 9222, timeout: float = 10.0,
                        submit: bool = True, transport: CdpTransport | None = None,
                        page_id: str = "") -> dict[str, Any]:
    """Insert ``text`` into the composer and submit it without OS input.

    ``transport`` is injectable so tests can exercise the DOM sequence without a
    live Electron process.  Production callers pass ``port`` only.
    """
    page: dict[str, Any] = {}
    own = transport is None
    if own:
        targets = _targets(port)
        chosen = _pick_page(targets, page_id)
        if not chosen:
            raise GuiAutomationError("GUI_SEND_FAILED", "CDP 没有可用的 page 目标")
        page = chosen
        transport = WebSocketTransport(
            page["webSocketDebuggerUrl"], origin=f"http://127.0.0.1:{int(port)}",
            timeout=timeout,
        )
    try:
        expression = INSERT_JS.replace(json.dumps("__TEXT__"),
                                       json.dumps(text, ensure_ascii=False))
        inserted = _evaluate(transport, expression, timeout) or {}
        if not inserted.get("ok"):
            raise GuiAutomationError(
                "GUI_SEND_FAILED",
                f"CDP 未写入输入框: {inserted.get('reason') or inserted}",
                details={"insert": inserted},
            )
        time.sleep(0.35)
        submitted: dict[str, Any] = {"ok": False, "reason": "skipped"}
        if submit:
            submitted = _evaluate(transport, SUBMIT_JS, timeout) or {}
            if not submitted.get("ok"):
                raise GuiAutomationError(
                    "GUI_SEND_FAILED",
                    f"CDP 未找到提交按钮: {submitted.get('reason') or submitted}",
                    details={"insert": inserted, "submit": submitted},
                )
        return {
            "ok": True, "path": "cdp-dom", "target": page.get("id", page_id),
            "chars": len(text), "insert": inserted, "submit": submitted,
        }
    finally:
        if own:
            transport.close()


__all__ = ["send_prompt_via_cdp", "wait_for_cdp", "WebSocketTransport",
           "INSERT_JS", "SUBMIT_JS"]
