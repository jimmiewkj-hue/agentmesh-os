"""User-visible restart protection and manual-submit blocking."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hub_test_support import default_plan, make_project

from agentloop_hub.adapters.base import ExecutionOutcome
from agentloop_hub.adapters.gui import GuiAutomationAdapter
from agentloop_hub.control import interfaces, runner as runner_module, trae_interface
from agentloop_hub.gui.cdp import submit_composer_via_cdp
from agentloop_hub.gui.base import GuiAutomationError
from agentloop_hub.util import write_json
from test_gui_adapter import make_agent, make_request


def _value(payload):
    return {"id": 1, "result": {"result": {"value": payload}}}


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def call(self, method, params, timeout=10.0):
        self.calls.append((method, params))
        return self.responses.pop(0)

    def close(self):
        pass


def test_restart_required_error_code_is_not_rewritten():
    error = GuiAutomationError("APP_RESTART_REQUIRED", "restart me")
    assert error.code == "APP_RESTART_REQUIRED"


def test_workbuddy_running_without_cdp_requires_explicit_restart(tmp_path, monkeypatch):
    exe = tmp_path / "WorkBuddy.exe"
    exe.write_bytes(b"stub")

    def _no_cdp(*args, **kwargs):
        raise OSError("port closed")

    calls = []
    monkeypatch.setattr(interfaces, "_local_urlopen", _no_cdp)
    monkeypatch.setattr("agentloop_hub.control.apps.process_running",
                        lambda image: True)
    monkeypatch.setattr(interfaces.subprocess, "run",
                        lambda *args, **kwargs: calls.append(args) or SimpleNamespace(
                            stdout="", returncode=0))

    out = interfaces.ensure_workbuddy_cdp(executable=str(exe), allow_restart=False)

    assert out.ok is False
    assert out.error_code == "APP_RESTART_REQUIRED"
    assert calls == []  # no taskkill, the user's app keeps running


def test_trae_running_without_cdp_requires_explicit_restart(tmp_path, monkeypatch):
    exe = tmp_path / "Trae CN.exe"
    exe.write_bytes(b"stub")
    monkeypatch.setattr(trae_interface, "_cdp_alive", lambda port: False)
    monkeypatch.setattr(trae_interface, "_process_running", lambda image: True)

    out = trae_interface.ensure_trae_cdp(
        config={"executable": str(exe)}, allow_restart=False)

    assert out.ok is False
    assert out.error_code == "APP_RESTART_REQUIRED"


def test_workbuddy_prefill_waits_for_marker_instead_of_placeholder():
    """The old loop broke on the 25-char welcome placeholder and killed the app."""
    transport = FakeTransport([
        _value({"ok": True, "chars": 25, "text": "今天帮你做些什么？"}),
        _value({"ok": True, "chars": 80,
                "text": "task text\n<!-- MARKER_abc -->"}),
        _value({"ok": True, "method": "button-click", "label": "发送"}),
    ])
    result = submit_composer_via_cdp(
        transport=transport, expect_contains="MARKER_abc",
        prefill_timeout=2.0, submit_interval=0,
    )
    assert result["path"] == "cdp-submit-prefilled"
    assert result["prefill"]["chars"] == 80
    assert result["submit"]["ok"] is True


def test_trae_submit_failure_blocks_and_writes_manual_action(tmp_path, monkeypatch):
    adapter = GuiAutomationAdapter(
        make_agent("qoder_cn"), config={"mode": "gui_automation",
                                         "gui_app": "trae_cn"})
    request = make_request(tmp_path)
    events: list[dict] = []
    request.notify = events.append
    log = tmp_path / "gui.log"

    def _fail(*args, **kwargs):
        raise GuiAutomationError("GUI_SEND_FAILED", "send button disabled")

    monkeypatch.setattr(trae_interface, "send_trae_prompt", _fail)
    result, manual = adapter._submit_prompt(
        request, {"method": "trae_state_vscdb"}, "MARKER_xyz", "task text",
        log, SimpleNamespace(app="trae_cn"))

    assert result["manual"] is True
    assert manual is not None
    assert manual["kind"] == "manual_submit"
    action_file = Path(request.task_dir) / "manual-action.json"
    assert action_file.exists()
    assert events[0]["event"] == "manual_action_required"
    assert events[0]["task_uid"] == request.task.uid

    adapter._end_manual(request, log, outcome="resolved", detail={})
    assert not action_file.exists()
    assert events[-1]["event"] == "manual_action_resolved"


def _result_payload(task_id: str, agent_id: str) -> dict:
    return {
        "task_id": task_id, "agent_id": agent_id, "status": "completed",
        "summary": "manual bridge completed", "findings": [], "files_read": [],
        "files_changed": [], "commands_run": [], "tool_calls": [], "tests": [],
        "evidence": [], "risks": [], "unresolved_items": [], "artifacts": [],
        "token_usage": {}, "cost_estimate": {},
        "recommended_next_action": [],
    }


def test_blocked_node_resumes_and_next_node_runs(tmp_path, monkeypatch):
    """A waiting node must hold the chain, then release it automatically."""
    repo, hub, scenario_path = make_project(tmp_path)
    hub.register_agent({
        "agent_id": "gui_blocker", "name": "GUI Blocker", "product": "Blocker",
        "adapter_type": "gui_automation", "access_level": "L3",
        "adapter_config": {"gui_app": "workbuddy"}, "models": ["block-model"],
        "capabilities": {"read_files": True, "structured_output": True,
                         "gui_control": True}, "status": "online",
    })
    release = threading.Event()

    class BlockingAdapter:
        def __init__(self, agent):
            self.agent = agent

        def execute(self, request):
            target = Path(request.task_dir) / "manual-action.json"
            write_json(target, {"kind": "manual_submit", "task_id": request.task.task_id,
                                "since": "test", "instructions": "click send"})
            request.notify({"event": "manual_action_required",
                            "task_uid": request.task.uid,
                            "kind": "manual_submit",
                            "message": "waiting for manual submit",
                            "instructions": "click send"})
            assert release.wait(10), "test did not release the blocked adapter"
            request.notify({"event": "manual_action_resolved",
                            "task_uid": request.task.uid, "outcome": "resolved"})
            return ExecutionOutcome(
                status="completed",
                result_payload=_result_payload(request.task.task_id, self.agent.agent_id),
            )

    real_create = runner_module.create_adapter

    def fake_create(agent):
        if agent.agent_id == "gui_blocker":
            return BlockingAdapter(agent)
        return real_create(agent)

    monkeypatch.setattr(runner_module, "create_adapter", fake_create)

    plan = {
        "plan_version": 1, "goal": "blocked chain",
        "constraints": [], "tasks": [
            {"task_id": "B001", "title": "block", "type": "analysis",
             "objective": "block then complete", "suggested_agent": "gui_blocker",
             "dependencies": [], "scope": [], "out_of_scope": [],
             "expected_outputs": ["summary"], "acceptance_criteria": ["done"],
             "verification_commands": [], "token_budget": 5000,
             "time_limit_minutes": 5, "retry_limit": 0, "risk_level": "low"},
            {"task_id": "B002", "title": "after", "type": "analysis",
             "objective": "read readme", "suggested_agent": "mock_worker",
             "dependencies": ["B001"], "scope": [], "out_of_scope": [],
             "expected_outputs": ["summary"], "acceptance_criteria": ["done"],
             "verification_commands": [], "token_budget": 5000,
             "time_limit_minutes": 5, "retry_limit": 0, "risk_level": "low"},
        ],
    }
    imported = hub.import_plan(plan, main_agent_id="mock_main", model="mock-model")
    plan_id = imported["plan_id"]
    hub.configure_chain(
        goal="blocked chain",
        stages=[{"task_id": "B001", "agent_id": "gui_blocker",
                 "model": "block-model", "type": "analysis"},
                {"task_id": "B002", "agent_id": "mock_worker",
                 "model": "mock-model", "type": "analysis"}],
        plan_id=plan_id, save_only=True,
    )
    for approval in hub.approvals_pending():
        if approval["kind"] == "architecture":
            hub.decide_approval(approval["approval_id"], approve=True)
    run_id = hub.approve_plan(plan_id)["run_id"]
    hub.start_run(run_id, max_parallel=1, background=True)

    deadline = time.time() + 15
    while time.time() < deadline:
        tasks = {t["task_id"]: t for t in hub.tasks(run_id=run_id)}
        if tasks["B001"]["status"] == "waiting_user":
            break
        time.sleep(0.2)
    tasks = {t["task_id"]: t for t in hub.tasks(run_id=run_id)}
    assert tasks["B001"]["status"] == "waiting_user", tasks["B001"]["error"]
    assert tasks["B002"]["status"] == "queued"
    assert hub.run_status(run_id)["run"]["status"] == "waiting_user"
    actions = hub.overview()["manual_actions"]
    assert actions and actions[0]["task_id"] == "B001"
    assert actions[0]["title"] == "block"

    release.set()
    deadline = time.time() + 25
    while time.time() < deadline:
        tasks = {t["task_id"]: t for t in hub.tasks(run_id=run_id)}
        if tasks["B001"]["status"] == "completed" and tasks["B002"]["status"] == "completed":
            break
        time.sleep(0.3)
    tasks = {t["task_id"]: t for t in hub.tasks(run_id=run_id)}
    assert tasks["B001"]["status"] == "completed", tasks["B001"]["error"]
    assert not tasks["B001"]["error"]
    assert tasks["B002"]["status"] == "completed", tasks["B002"]["error"]
    run = hub.run_status(run_id)["run"]
    assert not run.get("blocked_reason")
