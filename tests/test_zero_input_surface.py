from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentloop_hub.control.zero_input import (
    OsInputBlocked, current_policy, os_input_allowed, require_os_input, status,
)
from agentloop_hub.gui.cdp import SUBMIT_JS, send_prompt_via_cdp
from agentloop_hub.gui.base import GuiAutomationError


@pytest.fixture(autouse=True)
def _clean_input_env(monkeypatch):
    monkeypatch.delenv("AGENTMESH_DISABLE_GUI_INPUT", raising=False)
    monkeypatch.delenv("AGENTMESH_ALLOW_OS_INPUT", raising=False)
    monkeypatch.delenv("AGENTMESH_OS_INPUT_AUDIT", raising=False)


def test_zero_input_is_default_and_blocks_every_kind():
    assert current_policy().mode == "blocked"
    assert not os_input_allowed()
    for kind in ("clipboard", "keyboard", "mouse"):
        with pytest.raises(OsInputBlocked) as info:
            require_os_input(kind)
        assert info.value.code == "GUI_INPUT_FORBIDDEN"
        assert info.value.kind == kind
        assert info.value.details["policy"]["mode"] == "blocked"


def test_disable_switch_locks_even_with_diagnostic_allow(monkeypatch):
    monkeypatch.setenv("AGENTMESH_DISABLE_GUI_INPUT", "1")
    monkeypatch.setenv("AGENTMESH_ALLOW_OS_INPUT", "1")
    policy = current_policy()
    assert not policy.allowed and policy.disable_locked
    with pytest.raises(OsInputBlocked):
        require_os_input("keyboard")


def test_diagnostic_switch_allows_and_reports(monkeypatch):
    monkeypatch.setenv("AGENTMESH_ALLOW_OS_INPUT", "1")
    policy = require_os_input("mouse", detail="diagnostic")
    assert policy.allowed and policy.mode == "diagnostic"
    assert status()["os_input"] == "allowed-diagnostic"


def test_audit_file_records_block(monkeypatch, tmp_path):
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AGENTMESH_OS_INPUT_AUDIT", str(audit))
    with pytest.raises(OsInputBlocked):
        require_os_input("clipboard")
    assert "os_input_blocked" in audit.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="Win32 input synthesis is Windows-only")
def test_windows_driver_guard_raises_gui_forbidden(monkeypatch):
    from agentloop_hub.gui import windows
    monkeypatch.setenv("AGENTMESH_DISABLE_GUI_INPUT", "1")
    with pytest.raises(GuiAutomationError) as info:
        windows._key_event(0x0D)
    assert info.value.code == "GUI_INPUT_FORBIDDEN"
    with pytest.raises(GuiAutomationError) as info:
        windows._set_clipboard("hello")
    assert info.value.code == "GUI_INPUT_FORBIDDEN"


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def call(self, method: str, params: dict, timeout: float = 10.0):
        self.calls.append((method, params))
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _value(payload):
    return {"id": 1, "result": {"result": {"value": payload}}}


def test_cdp_dom_insert_and_submit_without_input_events():
    transport = FakeTransport([
        _value({"ok": True, "method": "execCommand.insertText", "tag": "DIV"}),
        _value({"ok": True, "method": "button-click", "label": "发送"}),
    ])
    result = send_prompt_via_cdp("生成结果", port=9222, transport=transport)
    assert result["path"] == "cdp-dom"
    assert result["insert"]["method"] == "execCommand.insertText"
    assert result["submit"]["method"] == "button-click"
    expressions = [params["expression"] for _, params in transport.calls]
    assert all("Input.dispatchKeyEvent" not in expr for expr in expressions)
    assert all("Input.insertText" not in expr for expr in expressions)
    assert "生成结果" in expressions[0]


def test_cdp_submit_excludes_voice_and_non_send_controls():
    assert "语音" in SUBMIT_JS and "voice" in SUBMIT_JS
    assert "发送" in SUBMIT_JS and "send" in SUBMIT_JS
    # 开始/start 不再是正向匹配词，否则会点到“开始语音输入”
    good_line = next(line for line in SUBMIT_JS.splitlines() if "const good" in line)
    assert "开始" not in good_line and "start" not in good_line


def test_cdp_missing_composer_is_explicit():
    transport = FakeTransport([_value({"ok": False, "reason": "NO_COMPOSER"})])
    with pytest.raises(GuiAutomationError) as info:
        send_prompt_via_cdp("x", transport=transport)
    assert info.value.code == "GUI_SEND_FAILED"
    assert "NO_COMPOSER" in info.value.message


def test_cdp_polls_until_send_button_enables():
    transport = FakeTransport([
        _value({"ok": True, "method": "execCommand.insertText"}),
        _value({"ok": False, "reason": "SEND_DISABLED"}),
        _value({"ok": False, "reason": "SEND_DISABLED"}),
        _value({"ok": True, "method": "button-click", "label": "发送消息"}),
    ])
    result = send_prompt_via_cdp("x", transport=transport, submit_interval=0)
    assert result["submit"]["ok"] is True
    assert result["submit"]["attempts"] == 3


def test_cdp_submit_prefilled_workbuddy_composer():
    from agentloop_hub.gui.cdp import submit_composer_via_cdp
    transport = FakeTransport([
        _value({"ok": True, "chars": 7, "text": "task..."}),
        _value({"ok": True, "method": "button-click", "label": "发送"}),
    ])
    result = submit_composer_via_cdp(transport=transport, submit_interval=0)
    assert result["path"] == "cdp-submit-prefilled"
    assert result["prefill"]["chars"] == 7
    assert result["submit"]["label"] == "发送"
    expressions = [params["expression"] for _, params in transport.calls]
    assert all("Input." not in expr for expr in expressions)
    assert any("getClientRects" in expr for expr in expressions)
    assert any("bestText" in expr for expr in expressions)


def test_cdp_refuses_empty_prefilled_composer():
    from agentloop_hub.gui.cdp import submit_composer_via_cdp
    transport = FakeTransport([_value({"ok": True, "chars": 0})])
    with pytest.raises(GuiAutomationError) as info:
        # 0.05s keeps the probe to a single deterministic evaluate call.
        submit_composer_via_cdp(transport=transport, prefill_timeout=0.05)
    assert info.value.code == "GUI_SEND_FAILED"
    assert "空提交" in str(info.value)


def test_cdp_missing_submit_button_is_explicit():
    transport = FakeTransport([
        _value({"ok": True, "method": "execCommand.insertText"}),
        _value({"ok": False, "reason": "NO_SUBMIT"}),
    ])
    with pytest.raises(GuiAutomationError) as info:
        send_prompt_via_cdp("x", transport=transport, submit_retries=1)
    assert info.value.code == "GUI_SEND_FAILED"
    assert info.value.details["submit"]["reason"] == "NO_SUBMIT"


def _record(agent_id, **overrides):
    base = dict(
        agent_id=agent_id, name=agent_id, product=agent_id, edition="",
        adapter_type="gui_automation", adapter_config={}, models=[],
        extra={"installed": True}, status="online", version="test",
        data_path=Path(""),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _write_db(path: Path, statements: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def test_surface_report_readbacks_are_attached(tmp_path):
    from agentloop_hub.adapters.discovery import DiscoveryContext
    from agentloop_hub.control.agent_surface import surface_report

    _write_db(tmp_path / ".workbuddy" / "workbuddy.db", [
        "create table sessions (cwd text, model text, permission_mode text, "
        "source_mode text, status text)",
        "insert into sessions values ('D:/agentmesh-e2e/wb', 'glm-5.3-flash', "
        "'fullAccess', 'craft', 'completed')",
    ])
    qdb = tmp_path / "AppData" / "Roaming" / "com.qodercn.app.stable" / "main.sqlite"
    _write_db(qdb, [
        "create table chat_sessions (session_id text, cwd text, model text, "
        "permission_mode text, product_mode text, updated_at integer)",
        "insert into chat_sessions values ('s1', 'D:/agentmesh-e2e/q', "
        "'dfmodel', 'auto', 'coding', 100)",
        "create table workspaces (path text)",
        "insert into workspaces values ('D:/agentmesh-e2e/q')",
    ])
    trae_db = tmp_path / "trae.db"
    trae_db.write_bytes(bytes.fromhex("3115fc31b62b524142ecb128be09d1f4"))

    records = [
        _record("workbuddy", adapter_config={"executable": "C:/WB/WorkBuddy.exe"}),
        _record("qoder_cn", adapter_config={"executable": "C:/Q/Qoder CN.exe"}),
        _record("trae", adapter_config={"data_path": str(trae_db)}),
    ]
    ctx = DiscoveryContext(home=tmp_path, appdata=tmp_path / "AppData" / "Roaming",
                           localappdata=tmp_path / "AppData" / "Local",
                           programfiles=Path("C:/Program Files"))
    report = surface_report(records=records, ctx=ctx)

    by_id = {a["agent_id"]: a for a in report["agents"]}
    assert report["zero_input"]["mode"] == "blocked"
    wb = by_id["workbuddy"]
    assert wb["capabilities"]["workspace"]["status"] == "verified"
    assert wb["capabilities"]["workspace"]["evidence"]["cwd"] == "D:/agentmesh-e2e/wb"
    assert wb["capabilities"]["dev_mode"]["evidence"]["permission_mode"] == "fullAccess"
    qc = by_id["qoder_cn"]
    assert qc["capabilities"]["workspace"]["status"] == "verified"
    assert qc["capabilities"]["model"]["evidence"]["model"] == "dfmodel"
    trae = by_id["trae"]
    assert trae["capabilities"]["workspace"]["status"] == "unsupported"
    assert trae["capabilities"]["workspace"]["evidence"]["sqlite_magic"] is False
    assert all(agent["zero_input"] for agent in report["agents"])


def test_surface_report_requires_evidence_for_verified_cells(tmp_path):
    from agentloop_hub.adapters.discovery import DiscoveryContext
    from agentloop_hub.control.agent_surface import surface_report

    ctx = DiscoveryContext(home=tmp_path, appdata=tmp_path, localappdata=tmp_path,
                           programfiles=Path("C:/Program Files"))
    report = surface_report(records=[_record("workbuddy")], ctx=ctx)
    for cap in report["agents"][0]["capabilities"].values():
        if cap["status"] == "verified":
            assert cap["evidence"], cap
            assert cap["zero_input"] is True


def test_cdp_rejects_placeholder_prefill_when_marker_expected():
    from agentloop_hub.gui.cdp import submit_composer_via_cdp
    transport = FakeTransport([
        _value({"ok": True, "chars": 25, "text": "今天帮你做些什么？@ 添加上下文"}),
    ])
    with pytest.raises(GuiAutomationError) as info:
        submit_composer_via_cdp(transport=transport, prefill_timeout=0.05,
                                expect_contains="AGENTMESH_GUI_MARKER_TEST")
    assert info.value.code == "GUI_SEND_FAILED"
    assert "占位符" in str(info.value)
