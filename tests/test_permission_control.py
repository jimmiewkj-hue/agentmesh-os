from __future__ import annotations

import json
from pathlib import Path

from agentloop_hub.adapters.base import ExecutionRequest
from agentloop_hub.config import HubConfig
from agentloop_hub.gui.permissions import (
    PermissionWatcher, _probe_and_click, ensure_trae_full_access,
    ensure_workbuddy_full_access,
)
from conftest import make_project


class FakeTransport:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def call(self, method, params, timeout=0):
        self.calls.append(params.get("expression", ""))
        value = self.values.pop(0) if self.values else {}
        return {"result": {"result": {"value": value}}}

    def close(self):
        pass


def test_full_control_default_and_persist(tmp_path):
    from agentloop_hub.hub import Hub

    repo, hub, _ = make_project(tmp_path)
    assert hub.config.full_control is True
    hub.set_full_control(False)
    assert hub.config.full_control is False
    stored = json.loads((Path(repo) / ".agentloop" / "config.json").read_text(encoding="utf-8"))
    assert stored["full_control"] is False
    hub.close()

    fresh = Hub(repo, auto_discover=False)
    assert fresh.config.full_control is False
    fresh.close()


def test_execution_request_defaults_to_full_control():
    # The field is opt-out; the WebUI checkbox is checked by default.
    assert ExecutionRequest.__dataclass_fields__["full_control"].default is True
    assert HubConfig().full_control is True


def test_permission_probe_clicks_observed_label_without_os_input():
    fake = FakeTransport([{"ok": True, "clicked": {
        "label": "本次会话内始终允许", "text": "本次会话内始终允许",
        "tag": "DIV", "cls": "cr-permission-option",
        "container": "cr-permission-dialog",
    }}])
    result = _probe_and_click(9223, ["本次会话内始终允许", "允许读取"],
                              transport=fake)
    assert result["ok"] is True
    assert result["clicked"]["label"] == "本次会话内始终允许"
    expression = fake.calls[0]
    assert "Input." not in expression
    assert "本次会话内始终允许" in expression


def test_trae_full_access_clicks_menu_item_and_confirmation():
    fake = FakeTransport([
        {"ok": False, "reason": "NO_CONFIRM"},
        {"ok": True, "mode": "手动审批", "text": ""},
        True,
        {"ok": True, "text": "完全访问\n不经审批，直接在本机运行"},
        {"ok": True, "label": "启用", "dialog": "启用完全访问？\n取消\n启用"},
        {"mode": "完全访问"},
    ])
    result = ensure_trae_full_access(port=9342, transport=fake)
    assert result["ok"] is True, result
    assert result["confirm"]["label"] == "启用"
    assert result["mode"] == "完全访问"
    assert all("Input." not in expr for expr in fake.calls)
    assert any("启用" in expr for expr in fake.calls)


def test_workbuddy_full_access_turns_on_permission_switch():
    fake = FakeTransport([
        {"ok": True, "text": "默认权限", "full": False},
        True,
        {"ok": True, "before": False, "after": True},
        True,
        {"text": "允许完全访问"},
    ])
    result = ensure_workbuddy_full_access(port=9223, transport=fake)
    assert result["ok"] is True, result
    assert "允许完全访问" in result["mode"]
    assert all("Input." not in expr for expr in fake.calls)


def test_permission_watcher_records_auto_accept(tmp_path):
    log = tmp_path / "gui.log"
    accepted = []

    def clicker(app, port):
        accepted.append(app)
        return {"ok": True, "clicked": {"label": "允许读取", "tag": "BUTTON",
                                        "cls": "permission-button",
                                        "container": "permission-dialog"}}

    watcher = PermissionWatcher(app="workbuddy", port=9223, log_path=log,
                                interval=0.01, clicker=clicker)
    watcher.start()
    import time
    deadline = time.time() + 1.0
    while time.time() < deadline and not watcher.accepted:
        time.sleep(0.02)
    watcher.stop()
    assert watcher.accepted
    assert accepted
    lines = log.read_text(encoding="utf-8").splitlines()
    assert any("permission_auto_accept" in line for line in lines)
