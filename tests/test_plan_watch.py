"""Plan-phase completion watches and import hardening.

Reproduces the 2026-11-11 manual test defect: a GUI main agent returns
waiting_user, the job ends, and the plan the agent writes later (even after a
server restart) must still land in the approval pipeline without a second
agent generation.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from agentloop_hub.adapters.base import ExecutionOutcome
from agentloop_hub.control import planning as planning_mod
from agentloop_hub.control.planning import PlanningService
from agentloop_hub.errors import ValidationError
from hub_test_support import default_plan, make_project


class StubAdapter:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        return self.outcome


def waiting_outcome():
    return ExecutionOutcome(
        status="waiting_user",
        error="MANUAL_ACTION_REQUIRED: 菜单中没有 DeepSeek-V4.1-Flash",
        extras={"manual_action": {
            "kind": "agent_not_running", "message": "菜单中没有 DeepSeek-V4.1-Flash",
            "instructions": "请手动发送", "timeout_seconds": 1800,
        }},
    )


@pytest.fixture()
def no_watch_threads(monkeypatch):
    """Register watches without background threads for deterministic polls."""
    def fake_start(self, watch):
        stop = threading.Event()
        with self._watches_lock:
            self._watches[watch["plan_id"]] = {"watch": watch, "stop": stop}
    monkeypatch.setattr(PlanningService, "_start_watch_thread", fake_start)


def read_watch(hub, plan_id):
    path = Path(hub.paths.planning) / plan_id / "watch.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_waiting_user_registers_persistent_watch(tmp_path, monkeypatch, no_watch_threads):
    repo, hub, _ = make_project(tmp_path)
    stub = StubAdapter(waiting_outcome())
    monkeypatch.setattr(planning_mod, "create_adapter", lambda agent: stub)
    result = hub.request_plan("需要人工发送", main_agent_id="mock_main")
    assert result["status"] == "waiting_user"
    assert result["plan_id"], "waiting_user 必须带回 plan_id 供 WebUI 跟踪"
    assert result["result_path"]
    assert result["manual_action"]["instructions"] == "请手动发送"
    assert result["watch"]["status"] == "watching"
    watch = read_watch(hub, result["plan_id"])
    assert watch["status"] == "watching"
    assert watch["agent_plan_path"] == result["result_path"]
    assert watch["deadline_epoch"] > time.time()
    assert stub.calls == 1
    hub.close()


def test_watch_finalizes_written_plan_without_new_generation(tmp_path, monkeypatch, no_watch_threads):
    repo, hub, _ = make_project(tmp_path)
    stub = StubAdapter(waiting_outcome())
    monkeypatch.setattr(planning_mod, "create_adapter", lambda agent: stub)
    result = hub.request_plan("目标", main_agent_id="mock_main")
    plan_id = result["plan_id"]
    Path(result["result_path"]).write_text(
        json.dumps(default_plan(str(repo)), ensure_ascii=False), encoding="utf-8")
    watch = read_watch(hub, plan_id)
    assert hub.planning._poll_watch_once(watch) is True
    plan = hub.plan(plan_id)
    assert plan["status"] == "pending_approval"
    assert [t["task_id"] for t in plan["tasks"]] == ["T001", "T002", "T003"]
    assert hub.approvals_pending()[0]["kind"] == "plan"
    assert read_watch(hub, plan_id)["status"] == "completed"
    activity = json.loads((Path(hub.paths.planning) / plan_id / "activity.json")
                          .read_text(encoding="utf-8"))
    assert activity["status"] == "pending_approval"
    assert activity["watched"] is True
    assert stub.calls == 1, "监视器不得再次调用 Agent（不新开生成）"
    assert any(e["action"] == "plan.generated" for e in hub.audit_events())
    hub.close()


def test_watch_invalid_plan_does_not_repair(tmp_path, monkeypatch, no_watch_threads):
    repo, hub, _ = make_project(tmp_path)
    stub = StubAdapter(waiting_outcome())
    monkeypatch.setattr(planning_mod, "create_adapter", lambda agent: stub)
    result = hub.request_plan("目标", main_agent_id="mock_main")
    plan_id = result["plan_id"]
    invalid = {"plan_version": 1, "goal": "bad", "tasks": [{
        "task_id": "T1", "title": "x", "objective": "y",
        "type": "implementation", "scope": [], "dependencies": [],
    }]}
    Path(result["result_path"]).write_text(json.dumps(invalid), encoding="utf-8")
    assert hub.planning._poll_watch_once(read_watch(hub, plan_id)) is True
    watch = read_watch(hub, plan_id)
    assert watch["status"] == "invalid"
    assert "校验" in watch["error"]
    assert stub.calls == 1, "校验失败也不得自动发起修复生成"
    with pytest.raises(Exception):
        hub.plan(plan_id)
    hub.close()


def test_watch_resumes_after_restart(tmp_path, monkeypatch, no_watch_threads):
    repo, hub, _ = make_project(tmp_path)
    stub = StubAdapter(waiting_outcome())
    monkeypatch.setattr(planning_mod, "create_adapter", lambda agent: stub)
    result = hub.request_plan("目标", main_agent_id="mock_main")
    plan_id = result["plan_id"]
    hub.close()

    from agentloop_hub.hub import Hub
    hub2 = Hub(repo, auto_discover=False)
    assert plan_id in hub2.planning._watches, "Hub 启动时应自动恢复持久监视"
    assert hub2.planning.resume_watches() == 0
    Path(result["result_path"]).write_text(
        json.dumps(default_plan(str(repo)), ensure_ascii=False), encoding="utf-8")
    assert hub2.planning._poll_watch_once(read_watch(hub2, plan_id)) is True
    assert hub2.plan(plan_id)["status"] == "pending_approval"
    assert read_watch(hub2, plan_id)["status"] == "completed"
    hub2.close()


def test_import_supersedes_watch(tmp_path, monkeypatch, no_watch_threads):
    repo, hub, _ = make_project(tmp_path)
    stub = StubAdapter(waiting_outcome())
    monkeypatch.setattr(planning_mod, "create_adapter", lambda agent: stub)
    result = hub.request_plan("目标", main_agent_id="mock_main")
    plan_id = result["plan_id"]
    imported = hub.import_plan(default_plan(str(repo)))
    assert imported["status"] == "pending_approval"
    assert read_watch(hub, plan_id)["status"] == "superseded"
    hub.close()


def test_replan_supersedes_previous_watch(tmp_path, monkeypatch, no_watch_threads):
    repo, hub, _ = make_project(tmp_path)
    stub = StubAdapter(waiting_outcome())
    monkeypatch.setattr(planning_mod, "create_adapter", lambda agent: stub)
    first = hub.request_plan("目标", main_agent_id="mock_main")
    second = hub.request_plan("目标", main_agent_id="mock_main")
    assert first["plan_id"] != second["plan_id"]
    assert read_watch(hub, first["plan_id"])["status"] == "superseded"
    assert read_watch(hub, second["plan_id"])["status"] == "watching"
    hub.close()


def test_watch_expires(tmp_path, monkeypatch, no_watch_threads):
    repo, hub, _ = make_project(tmp_path)
    stub = StubAdapter(waiting_outcome())
    monkeypatch.setattr(planning_mod, "create_adapter", lambda agent: stub)
    result = hub.request_plan("目标", main_agent_id="mock_main")
    plan_id = result["plan_id"]
    watch = read_watch(hub, plan_id)
    watch["deadline_epoch"] = time.time() - 1
    assert hub.planning._poll_watch_once(watch) is True
    assert read_watch(hub, plan_id)["status"] == "expired"
    hub.close()


def test_watch_thread_end_to_end(tmp_path, monkeypatch):
    repo, hub, _ = make_project(tmp_path)
    stub = StubAdapter(waiting_outcome())
    monkeypatch.setattr(planning_mod, "create_adapter", lambda agent: stub)
    result = hub.request_plan("目标", main_agent_id="mock_main")
    plan_id = result["plan_id"]
    Path(result["result_path"]).write_text(
        json.dumps(default_plan(str(repo)), ensure_ascii=False), encoding="utf-8")
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if hub.plan(plan_id)["status"] == "pending_approval":
                break
        except Exception:
            time.sleep(0.3)
    else:
        pytest.fail("监视线程未在 15s 内接收计划")
    assert read_watch(hub, plan_id)["status"] == "completed"
    hub.close()


def test_list_watches_api_shape(tmp_path, monkeypatch, no_watch_threads):
    repo, hub, _ = make_project(tmp_path)
    stub = StubAdapter(waiting_outcome())
    monkeypatch.setattr(planning_mod, "create_adapter", lambda agent: stub)
    result = hub.request_plan("目标", main_agent_id="mock_main")
    watches = hub.plan_watches()
    assert watches and watches[0]["plan_id"] == result["plan_id"]
    assert watches[0]["remaining_seconds"] > 0
    hub.close()


# -- import hardening ------------------------------------------------------
def test_import_fenced_json_text(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    text = "```json\n" + json.dumps(default_plan(str(repo)), ensure_ascii=False) + "\n```"
    result = hub.import_plan(text)
    assert result["status"] == "pending_approval"
    hub.close()


def test_import_raw_json_string(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    result = hub.import_plan(json.dumps(default_plan(str(repo)), ensure_ascii=False))
    assert result["status"] == "pending_approval"
    hub.close()


def test_import_bom_and_relative_path(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    rel = ".agentloop/planning/manual/plan.json"
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(default_plan(str(repo)), ensure_ascii=False),
                      encoding="utf-8-sig")
    result = hub.import_plan(rel)
    assert result["status"] == "pending_approval"
    hub.close()


def test_import_bad_json_gives_parse_error(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    with pytest.raises(ValidationError) as exc:
        hub.import_plan("{not json")
    assert "解析失败" in exc.value.message
    hub.close()


def test_import_missing_file_gives_actionable_error(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    with pytest.raises(ValidationError) as exc:
        hub.import_plan("no/such/plan.json")
    assert "无法读取计划文件" in exc.value.message
    assert "工作区根目录" in exc.value.message
    hub.close()


def test_import_empty_gives_clear_error(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    with pytest.raises(ValidationError):
        hub.import_plan("")
    hub.close()

def test_timeout_outcome_salvages_written_plan(tmp_path, monkeypatch):
    """Trae's hollow session state makes wait_reply time out even though the
    plan file was written long before; the timeout must salvage, not fail."""
    repo, hub, _ = make_project(tmp_path)

    class TimeoutAdapter:
        def execute(self, request):
            Path(request.result_path).write_text(
                json.dumps(default_plan(str(repo)), ensure_ascii=False),
                encoding="utf-8")
            return ExecutionOutcome(
                status="timeout", error="GUI_TIMEOUT: 等待 Trae 回复超时（900.0s）")
    monkeypatch.setattr(planning_mod, "create_adapter", lambda agent: TimeoutAdapter())
    result = hub.request_plan("目标", main_agent_id="mock_main")
    assert result["status"] == "pending_approval", result
    assert any(e["action"] == "plan.salvaged" for e in hub.audit_events())
    hub.close()
