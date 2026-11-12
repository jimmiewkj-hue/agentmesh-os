"""Trae workspace-trust dialog handling (2026-11-07 real-chain defect #1).

Every freshly opened Trae folder raises "是否信任此文件夹"; the mask blocked
workspace registration and the model menu until an operator clicked
"是，我信任此作者".  These tests exercise the CDP probe/click sequence with a
scripted renderer, plus the dispatch integration points.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agentloop_hub.control import trae_interface as iface
from agentloop_hub.gui import cdp
from agentloop_hub.gui.base import GuiAutomationError


class FakeTransport:
    def __init__(self, url, *, origin="", timeout=10.0):
        self.page_id = url.rsplit("/", 1)[-1]
        self.closed = False

    def call(self, method, params, timeout=10.0):
        raise AssertionError("FakeTransport.call should not be used directly")

    def close(self):
        self.closed = True


@pytest.fixture
def world(monkeypatch):
    state = {
        "dialogs": {},          # page_id -> probe result
        "clicks": [],           # (page_id, label)
        "click_effective": True,
        "appear_after_passes": 0,
        "passes": 0,
    }

    def fake_targets(port):
        return [{"type": "page", "id": pid, "webSocketDebuggerUrl": f"ws://fake/{pid}"}
                for pid in ("P1", "P2")]

    def fake_evaluate(transport, expression, timeout):
        if expression == iface.TRAE_TRUST_PROBE_JS:
            state["passes"] += 1
            if state["passes"] <= state["appear_after_passes"]:
                return {"found": False}
            return state["dialogs"].get(transport.page_id, {"found": False})
        if expression.startswith("((label) =>"):
            label = json.loads(expression[expression.rfind("(") + 1:expression.rfind(")")])
            state["clicks"].append((transport.page_id, label))
            if state["click_effective"]:
                state["dialogs"].pop(transport.page_id, None)
            return {"ok": True, "label": label}
        raise AssertionError(f"unexpected expression: {expression[:80]}")

    monkeypatch.setattr(cdp, "_targets", fake_targets)
    monkeypatch.setattr(cdp, "WebSocketTransport", FakeTransport)
    monkeypatch.setattr(cdp, "_evaluate", fake_evaluate)
    return state


def trust_dialog(label="是，我信任此作者"):
    return {
        "found": True,
        "text": "是否信任此文件夹中的文件的作者?",
        "buttons": [{"label": label, "x": 10, "y": 20},
                    {"label": "否，我不信任此作者", "x": 30, "y": 20}],
        "target": {"label": label, "x": 10, "y": 20},
    }


def test_trust_dialog_dismissed_with_affirmative_button(world):
    world["dialogs"]["P2"] = trust_dialog()
    result = iface.dismiss_trae_trust_dialog(9342)
    assert result["ok"] is True
    assert result["dismissed"] is True
    assert result["page_id"] == "P2"
    assert world["clicks"] == [("P2", "是，我信任此作者")]
    assert "P2" not in world["dialogs"]


def test_no_dialog_is_a_noop(world):
    result = iface.dismiss_trae_trust_dialog(9342)
    assert result == {"ok": True, "dismissed": False, "reason": "NO_TRUST_DIALOG"}
    assert world["clicks"] == []


def test_dialog_without_affirmative_button_is_never_clicked(world):
    dialog = trust_dialog()
    dialog["target"] = None
    world["dialogs"]["P1"] = dialog
    result = iface.dismiss_trae_trust_dialog(9342)
    assert result["ok"] is False
    assert result["reason"] == "TRUST_DIALOG_NO_CONFIRM"
    assert world["clicks"] == []


def test_ineffective_click_is_reported_not_hidden(world):
    world["dialogs"]["P1"] = trust_dialog()
    world["click_effective"] = False
    result = iface.dismiss_trae_trust_dialog(9342)
    assert result["ok"] is False
    assert result["reason"] == "TRUST_CLICK_INEFFECTIVE"
    assert world["clicks"] == [("P1", "是，我信任此作者")]


def test_polls_until_dialog_appears(world):
    world["dialogs"]["P1"] = trust_dialog()
    world["appear_after_passes"] = 2  # first full pass sees nothing
    result = iface.dismiss_trae_trust_dialog(9342, timeout_seconds=4.0)
    assert result["ok"] is True and result["dismissed"] is True


def test_pinned_page_is_probed_first(world):
    world["dialogs"]["P1"] = trust_dialog()
    world["dialogs"]["P2"] = trust_dialog()
    result = iface.dismiss_trae_trust_dialog(9342, page_id="P2")
    assert result["page_id"] == "P2"
    assert world["clicks"][0] == ("P2", "是，我信任此作者")


def test_open_workspace_clears_trust_then_registers(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    exe = tmp_path / "Trae CN.exe"
    exe.write_text("stub", encoding="utf-8")
    folder_id = hashlib.sha256(str(root.resolve()).lower().encode("utf-8")).hexdigest()[:24]
    cleared = {"done": False}

    def fake_dismiss(port, **kwargs):
        cleared["done"] = True
        return {"ok": True, "dismissed": True, "label": "是，我信任此作者"}

    class FakeState:
        def get(self, key, default=None):
            if key == "solo-lite.project-workspace-cache" and cleared["done"]:
                return {folder_id: {"identity": str(root.resolve())}}
            return default if default is not None else {}

    from agentloop_hub.control import apps
    monkeypatch.setattr(iface, "current_trae_workspace", lambda *a, **k: "")
    monkeypatch.setattr(iface, "_cdp_alive", lambda port: True)
    monkeypatch.setattr(iface, "dismiss_trae_trust_dialog", fake_dismiss)
    monkeypatch.setattr(iface, "read_state", lambda *a, **k: FakeState())
    monkeypatch.setattr(apps, "launch_detached", lambda *a, **k: 4242)
    outcome = iface.open_trae_workspace(
        app="trae_cn", workspace=str(root), executable=str(exe),
        timeout_seconds=5.0)
    assert outcome.ok, outcome.to_dict()
    assert cleared["done"] is True
    assert outcome.evidence["trust_dialog"]["dismissed"] is True


def test_select_model_blocks_on_unconfirmable_trust(monkeypatch):
    monkeypatch.setattr(iface, "resolve_trae_model",
                        lambda *a, **k: {"display_name": "DeepSeek-V4.1-Flash", "id": "x"})
    monkeypatch.setattr(iface, "dismiss_trae_trust_dialog",
                        lambda *a, **k: {"ok": False, "dismissed": False,
                                         "reason": "TRUST_DIALOG_NO_CONFIRM"})
    outcome = iface.select_trae_model_via_cdp("deepseek-v4.1-flash", port=9342)
    assert not outcome.ok
    assert outcome.error_code == "WORKSPACE_TRUST_REQUIRED"


def test_send_prompt_blocks_on_unconfirmable_trust(monkeypatch):
    monkeypatch.setattr(iface, "dismiss_trae_trust_dialog",
                        lambda *a, **k: {"ok": False, "dismissed": False,
                                         "reason": "TRUST_CLICK_INEFFECTIVE"})
    with pytest.raises(GuiAutomationError) as excinfo:
        iface.send_trae_prompt("task", port=9342)
    assert excinfo.value.code == "WORKSPACE_TRUST_REQUIRED"


def test_send_prompt_proceeds_when_dialog_dismissed(monkeypatch):
    monkeypatch.setattr(iface, "dismiss_trae_trust_dialog",
                        lambda *a, **k: {"ok": True, "dismissed": True})
    monkeypatch.setattr(iface, "pick_trae_page",
                        lambda *a, **k: {"id": "P1", "webSocketDebuggerUrl": "ws://fake/P1"})
    sent = {}

    def fake_send(text, **kwargs):
        sent["text"] = text
        return {"ok": True}

    def fake_evaluate(transport, expression, timeout):
        return {"ok": True, "method": "class-click"}

    monkeypatch.setattr(cdp, "send_prompt_via_cdp", fake_send)
    monkeypatch.setattr(cdp, "WebSocketTransport", FakeTransport)
    monkeypatch.setattr(cdp, "_evaluate", fake_evaluate)
    result = iface.send_trae_prompt("task body", port=9342)
    assert sent["text"] == "task body"
    assert result["submit"]["ok"] is True