"""演练操作员工具：用现有 gui/cdp.py 通道驱动本地 Chrome 里的 AgentMesh WebUI。

只用 Runtime.evaluate / Page.navigate / Page.captureScreenshot；
不发 Input.* 事件、不动 OS 鼠标键盘。非产品代码，仅本次实战演练使用。

用法:
  python webui_driver.py eval  "<JS 表达式>"     # 或从 stdin 读 JS
  python webui_driver.py shot  <输出.png>
  python webui_driver.py nav   <url>
  python webui_driver.py pages
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agentloop_hub.gui.cdp import (  # noqa: E402
    WebSocketTransport, _targets,
)

PORT = 9222
URL_MARK = "127.0.0.1:8787"


def pick_page() -> dict:
    pages = [t for t in _targets(PORT)
             if t.get("type") == "page" and URL_MARK in t.get("url", "")]
    if not pages:
        raise SystemExit("ERROR: 没有找到 WebUI 页面目标")
    return pages[0]


def with_transport(fn):
    page = pick_page()
    transport = WebSocketTransport(page["webSocketDebuggerUrl"],
                                   origin=f"http://127.0.0.1:{PORT}", timeout=30)
    try:
        return fn(transport)
    finally:
        transport.close()


def evaluate(expr: str) -> dict:
    def run(transport):
        resp = transport.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True,
        }, timeout=180)
        result = resp.get("result", {})
        if result.get("exceptionDetails"):
            return {"ok": False, "exception": result["exceptionDetails"]}
        return {"ok": True, "value": result.get("result", {}).get("value")}
    return with_transport(run)


def screenshot(path: str) -> dict:
    def run(transport):
        resp = transport.call("Page.captureScreenshot",
                              {"format": "png", "captureBeyondViewport": False},
                              timeout=30)
        data = resp.get("result", {}).get("data", "")
        Path(path).write_bytes(base64.b64decode(data))
        return {"ok": True, "path": str(Path(path).resolve()),
                "bytes": len(base64.b64decode(data))}
    return with_transport(run)


def navigate(url: str) -> dict:
    def run(transport):
        transport.call("Page.navigate", {"url": url}, timeout=30)
        return {"ok": True, "url": url}
    return with_transport(run)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "eval"
    if cmd == "pages":
        out = {"ok": True, "pages": [
            {"id": t["id"], "url": t.get("url", ""), "title": t.get("title", "")}
            for t in _targets(PORT) if t.get("type") == "page"]}
    elif cmd == "shot":
        out = screenshot(sys.argv[2])
    elif cmd == "nav":
        out = navigate(sys.argv[2])
    else:
        expr = sys.argv[2] if len(sys.argv) > 2 else sys.stdin.read()
        out = evaluate(expr)
    print(json.dumps(out, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()