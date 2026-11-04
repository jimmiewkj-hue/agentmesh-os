"""Minimal Chrome DevTools Protocol channel for zero-mouse Qoder input.

The Qoder renderer is Electron/Chromium.  When the desktop is launched with
``--remote-debugging-port`` we can focus the composer and insert text through
the renderer's own input pipeline; no mouse event is generated.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

from .base import GuiAutomationError


def _targets(port: int) -> list[dict[str, Any]]:
    with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/json/list", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _call(ws: Any, call_id: int, method: str, params: dict[str, Any],
          timeout: float = 10.0) -> dict[str, Any]:
    ws.send(json.dumps({"id": call_id, "method": method, "params": params}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        message = json.loads(ws.recv())
        if message.get("id") == call_id:
            return message
    raise GuiAutomationError("GUI_SEND_FAILED", f"CDP {method} 超时")


def send_prompt_via_cdp(text: str, *, port: int = 9222,
                        timeout: float = 10.0) -> dict[str, Any]:
    try:
        import websocket  # type: ignore
    except Exception as exc:
        raise GuiAutomationError("GUI_SEND_FAILED", f"websocket-client 不可用: {exc}") from exc
    targets = _targets(port)
    page = next((item for item in targets
                 if item.get("type") == "page" and "renderer" in str(item.get("title"))), None)
    if not page:
        raise GuiAutomationError("GUI_SEND_FAILED", "CDP 没有 renderer 页面")
    ws = websocket.create_connection(page["webSocketDebuggerUrl"],
                                     timeout=timeout,
                                     origin=f"http://127.0.0.1:{port}")
    try:
        result = _call(ws, 1, "Runtime.evaluate", {
            "expression": (
                "(()=>{const e=document.querySelector('[contenteditable=\"true\"]')"
                "||document.querySelector('[role=\"textbox\"]');"
                "if(!e)return 'NO_COMPOSER';e.focus();return 'FOCUSED';})()"
            ),
            "returnByValue": True,
        })
        value = result.get("result", {}).get("result", {}).get("value")
        if value != "FOCUSED":
            raise GuiAutomationError("GUI_SEND_FAILED", f"CDP 未找到输入框: {value}")
        _call(ws, 2, "Input.insertText", {"text": text})
        for call_id, event_type in ((3, "keyDown"), (4, "keyUp")):
            _call(ws, call_id, "Input.dispatchKeyEvent", {
                "type": event_type, "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
                **({"text": "\r"} if event_type == "keyDown" else {}),
            })
        return {"ok": True, "path": "cdp", "target": page.get("id"),
                "chars": len(text)}
    finally:
        try:
            ws.close()
        except Exception:
            pass


__all__ = ["send_prompt_via_cdp"]
