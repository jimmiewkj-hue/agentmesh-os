from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agentloop_hub.adapters.base import ExecutionRequest
from agentloop_hub.adapters.discovery import DiscoveryContext, discover_agents
from agentloop_hub.adapters.gui import GuiAutomationAdapter
from agentloop_hub.connectors import is_auto_executable
from agentloop_hub.gui.base import GuiAutomationError, GuiReply
from agentloop_hub.models import AgentRecord, Task, TaskSpec


class StubDriver:
    def __init__(self, *, model_ok: bool = True, attach_error: str = ""):
        self.model_ok = model_ok
        self.attach_error = attach_error
        self.sent: list[str] = []
        self.selected: list[str] = []
        self.screenshots: list[str] = []

    def attach(self) -> dict[str, Any]:
        if self.attach_error:
            raise GuiAutomationError(self.attach_error, "stub attach failure")
        return {"title": "stub", "hwnd": 1, "rect": [0, 0, 100, 100]}

    def select_model(self, model_key: str) -> bool:
        self.selected.append(model_key)
        return self.model_ok

    def send_prompt(self, text: str) -> None:
        self.sent.append(text)

    def screenshot(self, path: str | Path) -> str:
        self.screenshots.append(str(path))
        return ""

    def close(self) -> None:
        pass


class StubReader:
    def __init__(self, reply: GuiReply | None = None, error: str = ""):
        self.reply = reply
        self.error = error
        self.marker = ""

    def wait_reply(self, marker: str, *, started_at: float, timeout: float,
                   stall_seconds: float, expected_model: str = "") -> GuiReply:
        self.marker = marker
        if self.error:
            raise GuiAutomationError(self.error, "stub reader failure")
        assert self.reply is not None
        return self.reply


def make_agent(app: str = "qoder_cn", *, status: str = "online") -> AgentRecord:
    return AgentRecord(
        agent_id="qoder_cn" if app == "qoder_cn" else "workbuddy",
        name=app, product=app, adapter_type="gui_automation", access_level="L3",
        status=status, adapter_config={"mode": "gui_automation", "gui_app": app},
        capabilities={"read_files": True, "write_files": True, "terminal": True,
                      "structured_output": True, "gui_control": True},
    )


def make_task(tmp_path: Path, *, task_id: str = "T1") -> Task:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    spec = TaskSpec(task_id=task_id, title="t", objective="o", type="implementation",
                    scope=["calc.py"], token_budget=1000, time_limit_minutes=1)
    return Task(uid=f"run:{task_id}", project_id="prj", plan_id="plan", run_id="run",
                spec=spec, status="queued", workdir=str(work),
                task_dir=str(tmp_path / ".task"), base_commit="abc")


def make_request(tmp_path: Path, *, purpose: str = "task",
                 result_name: str = "result.json") -> ExecutionRequest:
    task_dir = tmp_path / ".task"
    task_dir.mkdir(exist_ok=True)
    prompt = task_dir / "prompt.md"
    prompt.write_text("请完成 calc.py 修复并运行测试。", encoding="utf-8")
    return ExecutionRequest(
        project_id="prj", project_root=str(tmp_path), task=make_task(tmp_path),
        workdir=str(tmp_path / "work"), task_dir=str(task_dir),
        prompt_path=str(prompt), result_path=str(task_dir / result_name),
        model="DeepSeek-Flash", timeout_seconds=5, purpose=purpose,
    )


def test_gui_adapter_builds_strict_result_package(tmp_path):
    request = make_request(tmp_path)
    reader = StubReader(GuiReply(
        session_id="sess-1", text="已修复 calc.py，pytest 3 passed",
        model="dfmodel", transcript_path=str(tmp_path / "transcript.jsonl"),
        tool_calls=[{"name": "Bash", "input": {"command": "pytest -q"}}],
        usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    ))
    driver = StubDriver()
    adapter = GuiAutomationAdapter(
        make_agent(), driver=driver, reader=reader,
        config={"mode": "gui_automation", "gui_app": "qoder_cn",
                "allow_gui_model_fallback": True},
    )
    outcome = adapter.execute(request)

    assert outcome.status == "completed"
    assert outcome.error_code == ""
    payload = outcome.result_payload
    assert payload["task_id"] == "T1"
    assert payload["agent_id"] == "qoder_cn"
    assert payload["status"] == "completed"
    assert "已修复" in payload["summary"]
    assert payload["tool_calls"][0]["name"] == "Bash"
    assert payload["commands_run"][0]["command"] == "pytest -q"
    assert Path(outcome.result_path).exists()
    assert json.loads(Path(outcome.result_path).read_text(encoding="utf-8"))["task_id"] == "T1"
    assert outcome.usage.input_tokens == 100
    assert outcome.usage.output_tokens == 20
    assert outcome.session_id == "sess-1"
    assert outcome.extras["gui_app"] == "qoder_cn"
    assert outcome.extras["transcript"].endswith("transcript.jsonl")
    # Prompt carries a unique marker for transcript correlation.
    assert "AGENTMESH_GUI_MARKER_" in driver.sent[0]
    assert driver.sent[0].startswith("请完成 calc.py")
    # The GUI log is written to the task directory.
    assert (Path(request.task_dir) / "gui" / "gui.log").exists()


def test_gui_adapter_plan_extracts_fenced_json(tmp_path):
    request = make_request(tmp_path, purpose="plan", result_name="plan.json")
    plan = {
        "goal": "修复 calc.py",
        "tasks": [{
            "task_id": "T1", "title": "修复", "objective": "修复加法",
            "type": "implementation", "scope": ["calc.py"],
        }],
    }
    text = "计划如下：\n```json\n" + json.dumps(plan, ensure_ascii=False) + "\n```\n"
    reader = StubReader(GuiReply(session_id="sess-plan", text=text, model="dfmodel",
                                 transcript_path=str(tmp_path / "p.jsonl")))
    outcome = GuiAutomationAdapter(
        make_agent(), driver=StubDriver(), reader=reader,
        config={"mode": "gui_automation", "gui_app": "qoder_cn",
                "allow_gui_model_fallback": True},
    ).execute(request)
    assert outcome.status == "completed"
    written = json.loads(Path(request.result_path).read_text(encoding="utf-8"))
    assert written["goal"] == "修复 calc.py"
    assert written["tasks"][0]["task_id"] == "T1"


def test_gui_adapter_error_codes_are_stable(tmp_path):
    request = make_request(tmp_path)
    outcome = GuiAutomationAdapter(
        make_agent(), driver=StubDriver(attach_error="GUI_WINDOW_NOT_FOUND"),
        reader=StubReader(),
    ).execute(request)
    assert outcome.status == "failed"
    assert outcome.error_code == "GUI_WINDOW_NOT_FOUND"
    assert "GUI_WINDOW_NOT_FOUND" in outcome.error

    outcome = GuiAutomationAdapter(
        make_agent(), driver=StubDriver(), reader=StubReader(error="GUI_STALLED"),
        config={"mode": "gui_automation", "gui_app": "qoder_cn",
                "allow_gui_model_fallback": True},
    ).execute(request)
    assert outcome.status == "timeout"
    assert outcome.error_code == "GUI_STALLED"

    outcome = GuiAutomationAdapter(
        make_agent("workbuddy"), driver=StubDriver(model_ok=False),
        reader=StubReader(),
        config={"mode": "gui_automation", "gui_app": "workbuddy",
                "allow_gui_model_fallback": True},
    ).execute(make_request(tmp_path))
    assert outcome.status == "failed"
    assert outcome.error_code == "GUI_MODEL_SWITCH_FAILED"

    outcome = GuiAutomationAdapter(
        make_agent("workbuddy"), driver=StubDriver(),
        reader=StubReader(error="GUI_MODEL_SWITCH_FAILED"),
        config={"mode": "gui_automation", "gui_app": "workbuddy",
                "allow_gui_model_fallback": True},
    ).execute(make_request(tmp_path))
    assert outcome.error_code == "GUI_MODEL_SWITCH_FAILED"


def test_gui_adapter_model_mismatch_uses_reader_expected_model(tmp_path):
    request = make_request(tmp_path)
    request.model = "GLM-5.3-Flash"
    seen: dict[str, str] = {}

    class ModelReader(StubReader):
        def wait_reply(self, marker, *, started_at, timeout, stall_seconds, expected_model=""):
            seen["expected"] = expected_model
            raise GuiAutomationError("GUI_MODEL_SWITCH_FAILED", "mismatch")

    outcome = GuiAutomationAdapter(
        make_agent("workbuddy"), driver=StubDriver(), reader=ModelReader(),
        config={"mode": "gui_automation", "gui_app": "workbuddy",
                "allow_gui_model_fallback": True},
    ).execute(request)
    assert seen["expected"] == "glm-5.3-flash"
    assert outcome.error_code == "GUI_MODEL_SWITCH_FAILED"


def test_gui_model_click_requires_explicit_opt_in(tmp_path):
    outcome = GuiAutomationAdapter(
        make_agent(), driver=StubDriver(), reader=StubReader(),
        config={"mode": "gui_automation", "gui_app": "qoder_cn"},
    ).execute(make_request(tmp_path))
    assert outcome.status == "failed"
    assert outcome.error_code == "MODEL_GUI_FORBIDDEN"


def test_no_gui_mode_forbids_gui_model_fallback(tmp_path):
    request = make_request(tmp_path)
    adapter = GuiAutomationAdapter(
        make_agent(), driver=StubDriver(), reader=StubReader(),
        config={"mode": "gui_automation", "gui_app": "qoder_cn",
                "no_gui_fallback": True},
    )
    outcome = adapter.execute(request)
    assert outcome.status == "failed"
    assert outcome.error_code == "MODEL_GUI_FORBIDDEN"


def test_set_model_qoder_uses_db_transcript_path(tmp_path, monkeypatch):
    from agentloop_hub.adapters import gui as gui_module
    from agentloop_hub.control.interfaces import InterfaceOutcome

    calls = []

    def fake_prepare(*, workspace, model, config=None):
        calls.append((workspace, model))
        return InterfaceOutcome(True, "db_write", {"workspace": workspace, "model": model},
                                {"model": model}, evidence={"session_id": "sess-db"})

    monkeypatch.setattr(gui_module, "prepare_qoder_session", fake_prepare)
    adapter = GuiAutomationAdapter(make_agent(), config={
        "mode": "gui_automation", "gui_app": "qoder_cn",
        "no_gui_fallback": True,
    })
    result = adapter.set_model("DeepSeek-Flash", workspace=str(tmp_path / "ws"))
    assert result["ok"] is True
    assert result["path"] == "db+transcript"
    assert result["actual"] == "dfmodel"
    assert calls == [(str(tmp_path / "ws"), "dfmodel")]


def test_workbuddy_prepare_returns_selection_sync(tmp_path, monkeypatch):
    from agentloop_hub.adapters import gui as gui_module
    from agentloop_hub.control.interfaces import InterfaceOutcome

    class FakeReader:
        def active_sessions(self, **_kwargs):
            return []

    sync_calls = []

    def fake_sync(**kwargs):
        sync_calls.append(kwargs)
        return {"ok": True, "changed": False, "context": kwargs["context_length"],
                "effort": kwargs["capability_level"]}

    monkeypatch.setattr(gui_module, "create_reader", lambda *a, **k: FakeReader())
    monkeypatch.setattr(
        gui_module, "ensure_workbuddy_cdp",
        lambda **k: InterfaceOutcome(True, "cdp", {"port": 9223}, {}))
    monkeypatch.setattr(
        gui_module, "prepare_workbuddy_task",
        lambda **k: InterfaceOutcome(True, "deeplink", {}, {},
                                     evidence={"started_at": 1000.0}))
    monkeypatch.setattr(
        gui_module, "sync_workbuddy_session_profile",
        lambda **k: InterfaceOutcome(True, "db_write", {}, {"deferred": True},
                                     evidence={"deferred": True}))
    monkeypatch.setattr(gui_module, "workbuddy_user_id", lambda config: "uid-1")
    monkeypatch.setattr(
        "agentloop_hub.gui.cdp.sync_workbuddy_selection_preferences", fake_sync)

    request = make_request(tmp_path)
    request.model = "GLM-5.3-Flash"
    request.context_length = 600000
    request.capability_level = "max"
    adapter = GuiAutomationAdapter(
        make_agent("workbuddy"),
        config={"mode": "gui_automation", "gui_app": "workbuddy",
                "workbuddy_cdp_port": 9223},
    )
    info = adapter._prepare_interface(
        adapter._profile(), request, "prompt", "marker",
        tmp_path / "gui.log", 1000.0,
    )
    assert info["selection_preferences"]["ok"] is True
    assert sync_calls and sync_calls[0]["model_id"] == "glm-5.3-flash"
    assert info["profile_sync"]["actual"] == {"deferred": True}


def test_set_model_workbuddy_readback_mismatch(tmp_path, monkeypatch):
    from agentloop_hub.adapters import gui as gui_module
    from agentloop_hub.control import interfaces as iface
    from agentloop_hub.control.interfaces import InterfaceOutcome

    def fake_prepare(**kwargs):
        return InterfaceOutcome(
            True, "deeplink", {"model": kwargs["model"]},
            {"model": kwargs["model"]}, evidence={"started_at": 1000.0},
        )

    monkeypatch.setattr(gui_module, "prepare_workbuddy_task", fake_prepare)
    monkeypatch.setattr(iface, "workbuddy_db_path", lambda config: tmp_path / "wb.db")
    monkeypatch.setattr(
        iface, "_newest_workbuddy_session",
        lambda *args, **kwargs: {"model": "glm-5.2", "session_id": "sess-mismatch"},
    )

    class FakeClock:
        def __init__(self):
            self.now = 1000.0

        def time(self):
            self.now += 1.0
            return self.now

        def monotonic(self):
            self.now += 1.0
            return self.now

        @staticmethod
        def sleep(_seconds):
            return None

    monkeypatch.setattr(gui_module, "time", FakeClock())
    adapter = GuiAutomationAdapter(make_agent("workbuddy"), config={
        "mode": "gui_automation", "gui_app": "workbuddy",
        "no_gui_fallback": True,
    })
    with pytest.raises(GuiAutomationError) as info:
        adapter.set_model("GLM-5.3-Flash", workspace=str(tmp_path / "ws"))
    assert info.value.code == "MODEL_MISMATCH"


def test_set_model_without_workspace_is_unsupported(tmp_path):
    adapter = GuiAutomationAdapter(make_agent(), config={
        "mode": "gui_automation", "gui_app": "qoder_cn",
        "no_gui_fallback": True,
    })
    with pytest.raises(GuiAutomationError) as info:
        adapter.set_model("DeepSeek-Flash")
    assert info.value.code == "MODEL_SWITCH_UNSUPPORTED"


def test_auto_executable_gui_requires_online_agent_and_profile():
    online = make_agent(status="online")
    assert is_auto_executable(online) is True
    offline = make_agent(status="offline")
    assert is_auto_executable(offline) is False
    unconfigured = make_agent()
    unconfigured.adapter_config = {"mode": "gui_automation"}
    assert is_auto_executable(unconfigured) is False


def _touch(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_discovery_upgrades_qoder_cn_and_workbuddy_to_gui_automation(tmp_path, monkeypatch):
    home = tmp_path / "home"
    pf = tmp_path / "pf"
    ctx = DiscoveryContext(home=home, appdata=tmp_path / "appdata",
                           localappdata=tmp_path / "local", programfiles=pf,
                           path_lookup=lambda name: None)
    _touch(pf / "Qoder" / "Qoder CN" / "Qoder CN.exe")
    _touch(home / ".qoder-cn" / ".qoder-app-status.json",
           json.dumps({"version": "0.5.1", "product": "qodercn"}))
    _touch(home / ".qoder-cn" / ".models" / "u" / "catalog-v7", "QMC")
    _touch(home / ".qoder-cn" / "projects" / "p" / "s.jsonl",
           json.dumps({"type": "runtime-config", "model": "dfmodel"}) + "\n")
    _touch(pf / "WorkBuddy" / "WorkBuddy.exe")
    _touch(pf / "WorkBuddy" / "version", "37.10.3-43")
    _touch(home / ".workbuddy" / "projects" / "p" / "s.jsonl",
           json.dumps({"providerData": {"model": "deepseek-v4.1-flash"}}) + "\n")
    db = home / ".workbuddy" / "workbuddy.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT)")
    conn.execute("INSERT INTO sessions (id, model) VALUES ('s', 'glm-5.3-flash')")
    conn.commit()
    conn.close()
    monkeypatch.setattr("agentloop_hub.adapters.discovery.resolve_opencode_executable",
                        lambda: "")

    agents = {a.agent_id: a for a in discover_agents("prj", ctx)}
    qoder_cn = agents["qoder_cn"]
    wb = agents["workbuddy"]
    assert qoder_cn.adapter_type == "gui_automation"
    assert qoder_cn.adapter_config["gui_app"] == "qoder_cn"
    assert qoder_cn.access_level == "L3"
    assert qoder_cn.version == "0.5.1"
    assert {"dfmodel", "DeepSeek-Flash"}.issubset(set(qoder_cn.models))
    assert qoder_cn.capabilities["auto_executable"] is True
    assert is_auto_executable(qoder_cn) is True

    assert wb.adapter_type == "gui_automation"
    assert wb.adapter_config["gui_app"] == "workbuddy"
    assert wb.access_level == "L3"
    assert wb.version == "37.10.3-43"
    assert {"glm-5.3-flash", "GLM-5.3-Flash"}.issubset(set(wb.models))
    assert wb.capabilities["auto_executable"] is True
    assert is_auto_executable(wb) is True
    assert wb.adapter_config["model_item_offset_ratio"]["glm-5.3-flash"]
