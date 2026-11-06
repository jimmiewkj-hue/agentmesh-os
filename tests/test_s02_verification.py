"""S02 regression tests: verification must have real veto power."""

from __future__ import annotations

import json
from pathlib import Path

from hub_test_support import default_scenario, make_project


def _scenario(*, task_type: str = "implementation", commands: list[str] | None = None,
              status: str = "completed", unresolved: list | None = None,
              scope: list[str] | None = None) -> dict:
    base = default_scenario()
    base["plan"] = {
        "plan_version": 1, "goal": "verification gate", "constraints": [],
        "tasks": [{
            "task_id": "T1", "title": "gate task", "type": task_type,
            "objective": "produce a result", "suggested_agent": "mock_worker",
            "dependencies": [],
            "scope": scope if scope is not None else (
                ["calc.py"] if task_type == "implementation" else []),
            "out_of_scope": [],
            "expected_outputs": ["summary"], "acceptance_criteria": ["evidence"],
            "verification_commands": commands or [], "token_budget": 5000,
            "time_limit_minutes": 5, "retry_limit": 0, "risk_level": "low",
        }],
    }
    base["tasks"] = {
        "T1": {
            "write_files": ([{"path": "calc.py",
                              "content": "def add(a, b):\n    return a + b\n"}]
                            if task_type == "implementation" else []),
            "summary": "gate result", "status": status,
            "tests": [{"command": "unit", "result": "passed"}],
            "evidence": [{"file": "calc.py", "lines": "1-2", "description": "change"}],
            "unresolved_items": unresolved or [],
        },
    }
    base.setdefault("verification", {"checks": {"scope_check": "passed"}})
    return base


def _run(tmp_path: Path, scenario: dict) -> tuple[Path, object, dict]:
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    requested = hub.request_plan("verification gate", main_agent_id="mock_main",
                                 model="mock-model")
    approved = hub.decide_approval(requested["approval_id"], approve=True)
    run_id = approved["outcome"]["run_id"]
    outcome = hub.start_run(run_id, max_parallel=1)
    return repo, hub, {"run_id": run_id, "outcome": outcome}


def _task(hub, run_id: str) -> dict:
    return next(t for t in hub.tasks(run_id=run_id) if t["task_id"] == "T1")


def test_required_verification_command_failure_blocks_task(tmp_path: Path):
    _, hub, ctx = _run(tmp_path, _scenario(
        commands=['python -c "import sys; sys.exit(7)"']))
    task = _task(hub, ctx["run_id"])
    assert task["status"] == "failed", task
    assert "必需验证命令失败" in (task.get("error") or "") or \
        "验证" in (task.get("error") or "")
    assert ctx["outcome"]["status"] != "pending_acceptance"
    hub.close()


def test_partial_result_cannot_be_delivered(tmp_path: Path):
    _, hub, ctx = _run(tmp_path, _scenario(task_type="analysis", status="partial"))
    task = _task(hub, ctx["run_id"])
    assert task["status"] == "failed", task
    assert "partial" in (task.get("error") or "")
    assert ctx["outcome"]["status"] != "pending_acceptance"
    hub.close()


def test_optional_unresolved_item_does_not_block_delivery(tmp_path: Path):
    _, hub, ctx = _run(tmp_path, _scenario(
        task_type="analysis", status="completed",
        unresolved=["optional: document the change later"]))
    task = _task(hub, ctx["run_id"])
    assert task["status"] == "completed", task
    assert ctx["outcome"]["status"] == "pending_acceptance", ctx["outcome"]
    hub.close()


def test_blocking_unresolved_item_blocks_delivery(tmp_path: Path):
    _, hub, ctx = _run(tmp_path, _scenario(
        task_type="analysis", status="completed",
        unresolved=["broken login flow still fails"]))
    task = _task(hub, ctx["run_id"])
    assert task["status"] == "failed", task
    assert "未关闭" in (task.get("error") or "") or "必需问题" in (task.get("error") or "")
    hub.close()


def test_required_independent_verifier_missing_blocks_not_completes(tmp_path: Path):
    repo, hub, ctx = _run(tmp_path, _scenario(task_type="analysis"))
    # Remove every candidate verifier; the executor itself must never verify.
    for agent in hub.store.list_agents():
        if agent.agent_id not in ("mock_worker",):
            hub.remove_agent(agent.agent_id)
    # Re-run a fresh task under the same configuration.
    requested = hub.request_plan("verification gate", main_agent_id="mock_worker",
                                 model="mock-model")
    approved = hub.decide_approval(requested["approval_id"], approve=True)
    run_id = approved["outcome"]["run_id"]
    hub.start_run(run_id, max_parallel=1)
    task = _task(hub, run_id)
    assert task["status"] == "blocked", task
    assert "没有可用的验证 Agent" in (task.get("error") or "")
    hub.close()


def test_verification_commands_run_in_the_candidate_snapshot(tmp_path: Path):
    root = str((tmp_path / "repo").resolve())
    command = (
        'python -c "import os,sys; '
        f"sys.exit(0 if os.path.realpath(os.getcwd()) != os.path.realpath(r'{root}') "
        "else 7)\""
    )
    _, hub, ctx = _run(tmp_path, _scenario(
        task_type="review", commands=[command]))
    task = _task(hub, ctx["run_id"])
    assert task["status"] == "completed", task
    hub.close()


def test_verify_and_summary_have_attempt_identity(tmp_path: Path):
    _, hub, ctx = _run(tmp_path, _scenario(task_type="analysis"))
    run_id = ctx["run_id"]
    task = _task(hub, run_id)
    verify_attempts = hub.store.list_attempts(f"{run_id}:T1:verify")
    assert verify_attempts and verify_attempts[0]["purpose"] == "verify"
    summary_attempts = hub.store.list_attempts(f"{run_id}:SUMMARY")
    assert summary_attempts and summary_attempts[0]["purpose"] == "summary"
    assert task["status"] == "completed"
    hub.close()


def test_second_desktop_agent_is_a_valid_independent_verifier(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path)
    for agent in hub.store.list_agents():
        hub.remove_agent(agent.agent_id)

    def register(agent_id: str, app: str) -> None:
        hub.register_agent({
            "agent_id": agent_id, "name": agent_id, "product": app,
            "adapter_type": "gui_automation", "access_level": "L3",
            "adapter_config": {"mode": "gui_automation", "gui_app": app},
            "capabilities": {"read_files": True, "write_files": True,
                             "terminal": True, "structured_output": True},
            "models": [], "status": "online",
        })

    register("workbuddy", "workbuddy")
    register("trae_cn", "trae_cn")
    executor = hub.get_agent("workbuddy")
    picked = hub.runner._select_verifier(executor)
    assert picked is not None and picked.agent_id == "trae_cn"
    hub.close()


def test_verifier_prefers_a_free_model_when_not_pinned(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    agent = hub.get_agent("mock_worker")
    agent.models = ["premium/model", "opencode/flash-free"]
    assert hub.runner._verifier_model(agent) == "opencode/flash-free"
    agent.extra["selected_model"] = "premium/model"
    assert hub.runner._verifier_model(agent) == "premium/model"
    hub.close()


def test_operator_can_resubmit_reclassified_result_after_failure(tmp_path: Path):
    _, hub, ctx = _run(tmp_path, _scenario(
        task_type="analysis", status="completed",
        unresolved=["broken login flow still fails"]))
    task = _task(hub, ctx["run_id"])
    assert task["status"] == "failed", task
    stored = hub.store.get_result(task["uid"])["payload"]
    attempt = hub.store.get_attempt(task["active_attempt_id"])
    corrected = dict(stored)
    corrected["unresolved_items"] = ["非阻塞：人工目视复核留待最终验收"]
    corrected["risks"] = list(stored.get("risks") or []) + ["浏览器目视复核留待验收"]
    corrected["token_usage"] = 0
    corrected["cost_estimate"] = 0
    hub.submit_result(
        task["uid"], corrected,
        attempt_id=str(attempt["attempt_id"]),
        request_id=str(attempt["request_id"]),
    )
    after = _task(hub, ctx["run_id"])
    assert after["status"] == "completed", after
    assert after["verification_status"] in ("passed", "warning")
    events = [e["action"] for e in hub.audit_events()]
    assert "result.submitted" in events
    hub.close()


def test_human_can_submit_result_for_a_paused_task(tmp_path: Path):
    _, hub, ctx = _run(tmp_path, _scenario(
        task_type="analysis", status="completed",
        unresolved=["broken login flow still fails"]))
    task = _task(hub, ctx["run_id"])
    stored = hub.store.get_result(task["uid"])["payload"]
    attempt = hub.store.get_attempt(task["active_attempt_id"])
    hub.store.set_task_status(task["uid"], "paused", error="程序恢复暂停", force=True)
    corrected = dict(stored)
    corrected["unresolved_items"] = ["非阻塞：留待最终验收"]
    hub.submit_result(
        task["uid"], corrected,
        attempt_id=str(attempt["attempt_id"]),
        request_id=str(attempt["request_id"]),
    )
    after = _task(hub, ctx["run_id"])
    assert after["status"] == "completed", after
    hub.close()


def test_repair_requeues_failed_task_with_verifier_feedback(tmp_path: Path):
    _, hub, ctx = _run(tmp_path, _scenario(
        commands=['python -c "import sys; sys.exit(7)"']))
    task = _task(hub, ctx["run_id"])
    assert task["status"] == "failed", task
    out = hub.repair_task(task["uid"])
    assert out["status"] == "queued"
    feedback = Path(task["task_dir"]) / "repair-feedback.json"
    assert feedback.exists()
    data = json.loads(feedback.read_text(encoding="utf-8"))
    assert data["warnings"]
    assert any("必需验证命令失败" in w for w in data["warnings"])
    refreshed = _task(hub, ctx["run_id"])
    assert refreshed["status"] == "queued"
    assert refreshed["attempts"] == 0
    hub.close()


def test_verification_request_focuses_on_repair_findings(tmp_path: Path):
    from hub_test_support import make_project
    from agentloop_hub.models import Task, TaskSpec

    _, hub, _ = make_project(tmp_path)
    verifier = hub.store.get_agent("mock_verifier")
    task = Task(
        uid="run_x:T1", project_id=hub.project.project_id, plan_id="plan",
        run_id="run_x", spec=TaskSpec(task_id="T1", title="x", objective="x",
                                      type="implementation", scope=["a.py"]),
        status="failed", task_dir=str(tmp_path / "td"))
    prompt_path, _ = hub.runner.prompts.write_verification_request(
        run_id="run_x", task=task, verifier=verifier, model="mock-model",
        patch_path="", result_summary={"task_id": "T1", "summary": "fixed"},
        verification_commands=[], attempt_id="att_x",
        recheck={"summary": "bridge was along the river",
                 "warnings": ["bridge span axis must be z"]},
    )
    text = Path(prompt_path).read_text(encoding="utf-8")
    assert "复验重点" in text
    assert "bridge span axis must be z" in text
    hub.close()


def test_disabled_independent_verifier_skips_the_model_call(tmp_path: Path):
    from hub_test_support import make_project

    repo, hub, _ = make_project(tmp_path, scenario=_scenario(task_type="analysis"))
    hub.config.require_independent_verifier = False
    requested = hub.request_plan("verification gate", main_agent_id="mock_main",
                                 model="mock-model")
    approved = hub.decide_approval(requested["approval_id"], approve=True)
    run_id = approved["outcome"]["run_id"]
    hub.start_run(run_id, max_parallel=1)
    task = _task(hub, run_id)
    assert task["status"] == "completed", task
    attempts = hub.store.list_attempts(task["uid"])
    assert not [a for a in attempts if a.get("purpose") == "verify"]
    hub.close()


def test_adopt_completed_attempt_after_a_timeout(tmp_path: Path):
    from hub_test_support import default_plan, make_project
    from agentloop_hub.util import new_id, write_json

    repo, hub, _ = make_project(tmp_path)
    imported = hub.import_plan(default_plan(str(repo)), main_agent_id="mock_main",
                               model="mock-model")
    for approval in hub.approvals_pending():
        if approval["kind"] == "architecture":
            hub.decide_approval(approval["approval_id"], approve=True)
    run_id = hub.approve_plan(imported["plan_id"])["run_id"]
    uid = f"{run_id}:T001"
    task = hub.store.get_task(uid)
    first = "att_old"
    result_path = tmp_path / "old-result.json"
    payload = {
        "task_id": "T001", "agent_id": task.assigned_agent or "mock_worker",
        "status": "completed", "summary": "late review result",
        "findings": [], "files_read": [], "files_changed": [],
        "commands_run": [], "tool_calls": [], "tests": [], "evidence": [],
        "risks": [], "unresolved_items": [], "artifacts": [],
        "token_usage": {"total": 1}, "cost_estimate": {}, "recommended_next_action": [],
    }
    write_json(result_path, payload)
    hub.store.create_attempt(
        attempt_id=first, project_id=hub.project.project_id, task_uid=uid,
        run_id=run_id, attempt_number=1, request_id=new_id("req"),
        workdir=task.workdir or str(tmp_path), result_path=str(result_path))
    second = "att_active"
    hub.store.create_attempt(
        attempt_id=second, project_id=hub.project.project_id, task_uid=uid,
        run_id=run_id, attempt_number=2, request_id=new_id("req"),
        workdir=task.workdir or str(tmp_path), result_path=str(tmp_path / "missing.json"))
    hub.store.update_task(uid, active_attempt_id=second)
    hub.store.set_task_status(uid, "waiting_user", error="GUI timeout", force=True)
    out = hub.runner.adopt_attempt_result(uid, first)
    assert out["status"] == "completed", out
    refreshed = hub.store.get_task(uid)
    assert refreshed.active_attempt_id == first
    assert hub.store.get_attempt(second)["status"] == "canceled"
    hub.close()
