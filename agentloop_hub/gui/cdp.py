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


def _local_urlopen(url: str, *, timeout: float):
    """Open a loopback URL without the environment HTTP proxy.

    The desktop app runs inside environments that export ``HTTP_PROXY`` (this
    box points at a local sandbox proxy).  ``urllib`` honours it even for
    127.0.0.1 and turns a live CDP endpoint into ``502 Bad Gateway``, so every
    loopback probe must bypass proxies explicitly.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(url, timeout=timeout)


def _targets(port: int) -> list[dict[str, Any]]:
    with _local_urlopen(f"http://127.0.0.1:{int(port)}/json/list", timeout=5) as response:
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
  let sawDisabled = false;
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
  // Qoder CN exposes "开始语音输入" next to "发送消息"; a naive /开始|start/
  // match clicks the microphone instead of sending.  Exclude every non-send
  // composer control before matching the send affordance.
  const bad = /(stop|停止|cancel|取消|interrupt|中断|语音|voice|mic|microphone|听写|dictation|附件|attach|上下文|context|权限|permission|模型|model|文件|file|编辑|视图|帮助|工作区|workspace|任务|task)/i;
  const good = /(send|发送|submit|提交)/i;
  for (const btn of candidates) {
    const label = [
      btn.getAttribute('aria-label'), btn.getAttribute('title'),
      btn.getAttribute('data-testid'), btn.textContent,
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
    if (!label || bad.test(label)) continue;
    if (good.test(label)) {
      if (btn.disabled) { sawDisabled = true; continue; }
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
  return {ok: false, reason: sawDisabled ? 'SEND_DISABLED' : 'NO_SUBMIT'};
})()"""


COMPOSER_TEXT_JS = r"""(() => {
  const c = document.querySelector('[contenteditable="true"]')
    || document.querySelector('[role="textbox"]')
    || document.querySelector('textarea');
  if (!c) return {ok: false, reason: 'NO_COMPOSER'};
  const text = (c.innerText || c.value || '').trim();
  return {ok: true, chars: text.length, text: text.slice(0, 300)};
})()"""


def composer_text_via_cdp(port: int = 9223, *, timeout: float = 10.0,
                          transport: CdpTransport | None = None) -> dict[str, Any]:
    own = transport is None
    if own:
        chosen = _pick_page(_targets(port))
        if not chosen:
            raise GuiAutomationError("GUI_SEND_FAILED", "CDP 没有可用的 page 目标")
        transport = WebSocketTransport(chosen["webSocketDebuggerUrl"],
                                       origin=f"http://127.0.0.1:{int(port)}",
                                       timeout=timeout)
    try:
        return _evaluate(transport, COMPOSER_TEXT_JS, timeout) or {"ok": False}
    finally:
        if own:
            transport.close()


def submit_composer_via_cdp(port: int = 9223, *, timeout: float = 10.0,
                            prefill_timeout: float = 20.0,
                            submit_retries: int = 12,
                            submit_interval: float = 0.4,
                            transport: CdpTransport | None = None) -> dict[str, Any]:
    """Submit an already-prefilled composer (WorkBuddy deeplink path).

    The deeplink only calls ``prepareInput``; no send happens.  This function
    waits until the prompt text appears in the renderer and then clicks the
    composer's own send control.  No text is inserted and no key event is sent.
    """
    own = transport is None
    if own:
        chosen = _pick_page(_targets(port))
        if not chosen:
            raise GuiAutomationError("GUI_SEND_FAILED", "CDP 没有可用的 page 目标")
        transport = WebSocketTransport(chosen["webSocketDebuggerUrl"],
                                       origin=f"http://127.0.0.1:{int(port)}",
                                       timeout=timeout)
    try:
        deadline = time.time() + float(prefill_timeout)
        prefill: dict[str, Any] = {"ok": False, "chars": 0}
        while time.time() < deadline:
            prefill = _evaluate(transport, COMPOSER_TEXT_JS, timeout) or {"ok": False}
            if prefill.get("ok") and int(prefill.get("chars") or 0) > 0:
                break
            time.sleep(0.5)
        if not prefill.get("ok"):
            raise GuiAutomationError(
                "GUI_SEND_FAILED", f"CDP 未找到输入框: {prefill.get('reason')}",
                details={"prefill": prefill},
            )
        if int(prefill.get("chars") or 0) <= 0:
            raise GuiAutomationError(
                "GUI_SEND_FAILED", "deeplink 已打开但输入框为空，拒绝空提交",
                details={"prefill": prefill},
            )
        submitted: dict[str, Any] = {"ok": False, "reason": "SEND_DISABLED"}
        for attempt in range(max(1, int(submit_retries))):
            time.sleep(0.35 if attempt == 0 else submit_interval)
            submitted = _evaluate(transport, SUBMIT_JS, timeout) or {}
            if submitted.get("ok"):
                submitted["attempts"] = attempt + 1
                break
            if submitted.get("reason") not in ("SEND_DISABLED", "NO_SUBMIT"):
                break
        if not submitted.get("ok"):
            raise GuiAutomationError(
                "GUI_SEND_FAILED",
                f"CDP 未找到可用的提交按钮: {submitted.get('reason')}",
                details={"prefill": prefill, "submit": submitted},
            )
        return {"ok": True, "path": "cdp-submit-prefilled",
                "prefill": prefill, "submit": submitted}
    finally:
        if own:
            transport.close()


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
                        page_id: str = "", submit_retries: int = 12,
                        submit_interval: float = 0.4) -> dict[str, Any]:
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
        submitted: dict[str, Any] = {"ok": False, "reason": "skipped"}
        if submit:
            # React-style composers enable the send button after their own
            # state update; poll for up to ~5s instead of clicking a disabled
            # control once.
            for attempt in range(max(1, int(submit_retries))):
                time.sleep(0.35 if attempt == 0 else submit_interval)
                submitted = _evaluate(transport, SUBMIT_JS, timeout) or {}
                if submitted.get("ok"):
                    submitted["attempts"] = attempt + 1
                    break
                if submitted.get("reason") not in ("SEND_DISABLED", "NO_SUBMIT"):
                    break
            if not submitted.get("ok"):
                raise GuiAutomationError(
                    "GUI_SEND_FAILED",
                    f"CDP 未找到可用的提交按钮: {submitted.get('reason') or submitted}",
                    details={"insert": inserted, "submit": submitted},
                )
        return {
            "ok": True, "path": "cdp-dom", "target": page.get("id", page_id),
            "chars": len(text), "insert": inserted, "submit": submitted,
        }
    finally:
        if own:
            transport.close()


__all__ = ["send_prompt_via_cdp", "submit_composer_via_cdp", "composer_text_via_cdp",
           "wait_for_cdp", "WebSocketTransport", "INSERT_JS", "SUBMIT_JS"]
