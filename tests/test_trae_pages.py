"""Trae page pinning: send and read the same renderer."""

from __future__ import annotations

from agentloop_hub.control import trae_interface
from agentloop_hub.gui import cdp


class FakeTransport:
    def __init__(self, url, **kwargs):
        self.url = url

    def close(self):
        pass


def _page(pid: str) -> dict:
    return {"id": pid, "type": "page", "title": pid,
            "webSocketDebuggerUrl": f"ws://{pid}"}


def test_pick_trae_page_prefers_a_renderer_with_composer(monkeypatch):
    monkeypatch.setattr(cdp, "_targets",
                        lambda port: [_page("first"), _page("chat")])
    monkeypatch.setattr(cdp, "WebSocketTransport", FakeTransport)
    monkeypatch.setattr(cdp, "_evaluate",
                        lambda transport, expr, timeout: (
                            {"selector": True, "composer": True}
                            if transport.url.endswith("chat") else {}))
    chosen = trae_interface.pick_trae_page(9342)
    assert chosen["id"] == "chat"


def test_login_wall_is_detected(monkeypatch):
    monkeypatch.setattr(cdp, "_targets", lambda port: [_page("chat")])
    monkeypatch.setattr(cdp, "WebSocketTransport", FakeTransport)
    monkeypatch.setattr(cdp, "_evaluate",
                        lambda transport, expr, timeout: (
                            "Sign in to use AI Features\nContinue with GitHub"))
    assert trae_interface.trae_login_required(9342) is True


def test_conversation_text_falls_back_to_another_page(monkeypatch):
    monkeypatch.setattr(cdp, "_targets",
                        lambda port: [_page("stale"), _page("chat")])
    monkeypatch.setattr(cdp, "WebSocketTransport", FakeTransport)

    def _evaluate(transport, expr, timeout):
        if transport.url.endswith("stale"):
            raise TimeoutError("stale renderer")
        return {"ok": True, "text": "reply", "chars": 5}

    monkeypatch.setattr(cdp, "_evaluate", _evaluate)
    result = trae_interface.trae_conversation_text(
        port=9342, page_id="stale")
    assert result["page_id"] == "chat"
    assert result["text"] == "reply"


def test_visible_model_ignores_effort_suffix():
    assert trae_interface.visible_model_name(
        "DeepSeek-V4.1-Flash\nMax") == "DeepSeek-V4.1-Flash"
    assert trae_interface.visible_model_name(
        "DeepSeek-V4.1-Flash Max") == "DeepSeek-V4.1-Flash"
    assert trae_interface.visible_model_matches(
        "DeepSeek-V4.1-Flash\nMax", "DeepSeek-V4.1-Flash")
    assert not trae_interface.visible_model_matches(
        "GLM-5.3\nHigh", "DeepSeek-V4.1-Flash")
