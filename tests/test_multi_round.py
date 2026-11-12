from __future__ import annotations

import json
from pathlib import Path

from hub_test_support import make_project
from agentloop_hub.control.rounds import decide, signature_of
from agentloop_hub.models import Task, TaskSpec


def _run_plan(hub, goal="fix add"):
    result = hub.request_plan(goal, main_agent_id="mock_main", model="mock-model")
    assert result["status"] == "pending_approval", result
    approved = hub.decide_approval(result["approval_id"], approve=True)
    return approved["outcome"]["run_id"]


def _reject(next_actions, *, completion="还有未解决项", risks=None):
    return {
        "summary_version": 1,
        "completion": completion,
        "changes": [],
        "verification": "未通过",
        "risks": risks or list(next_actions),
        "merge_recommendation": "reject",
        "next_actions": list(next_actions),
        "goal_status": "continue",
    }


def _approve(completion="目标已达成"):
    return {
        "summary_version": 1,
        "completion": completion,
        "changes": ["calc.py"],
        "verification": "passed",
        "risks": [],
        "merge_recommendation": "approve",
        "next_actions": [],
        "goal_status": "complete",
    }


def _base_tasks():
    return {
        "T001": {"summary": "calc has bug"},
        "T002": {"summary": "readme ok"},
        "T003": {
            "write_files": [{"path": "calc.py",
                             "content": "def add(a, b):\n    return a + b\n"}],
            "summary": "fixed add",
            "tests": [{"command": "python -m unittest -q", "result": "passed"}],
            "evidence": [{"file": "calc.py", "lines": "1-2", "description": "return a + b"}],
        },
    }


def _round2_task(comment="second round"):
    return {
        "write_files": [{"path": "calc.py",
                         "content": f"def add(a, b):\n    # {comment}\n    return a + b\n"}],
        "summary": "round two fix",
        "tests": [{"command": "python -m unittest -q", "result": "passed"}],
        "evidence": [{"file": "calc.py", "lines": "1-3", "description": "round two fix"}],
    }


def test_rejected_round_auto_plans_and_completes_next_round(tmp_path):
    scenario = {
        "tasks": {**_base_tasks(), "R2_T001": _round2_task()},
        "summary_sequence": [
            _reject(["修复 calc.py 的 add 并运行 unittest"]),
            _approve(),
        ],
    }
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    hub.config.round_replan = False  # deterministic fallback planner for this test
    run_id = _run_plan(hub)
    result = hub.start_run(run_id, max_parallel=2)
    assert result["status"] == "pending_acceptance", result
    run = hub.store.get_run(run_id)
    assert int(run["round_number"]) == 2, run
    assert run["goal_status"] == "complete", run
    assert len(run["summary"]["rounds"]) == 2, run["summary"]
    task_ids = {t["task_id"] for t in hub.tasks(run_id=run_id)}
    assert {"R2_T001", "R2_REVIEW"} <= task_ids, task_ids
    assert all(t["status"] == "completed" for t in hub.tasks(run_id=run_id))
    actions = [e["action"] for e in hub.audit_events()]
    assert "round.continued" in actions
    assert "round.plan_appended" in actions
    planned = next(e for e in hub.audit_events() if e["action"] == "round.plan_appended")
    assert planned["payload"]["source"] == "fallback"
    rounds_dir = Path(hub.paths.run_dir(run_id)) / "rounds"
    assert (rounds_dir / "round-1.json").exists()
    assert (rounds_dir / "round-2.json").exists()
    accepted = hub.decide_approval(result["approval_id"], approve=True)
    assert accepted["outcome"]["status"] == "pending_merge", accepted
    merged = hub.decide_approval(accepted["outcome"]["approval_id"], approve=True)
    assert merged["outcome"]["status"] == "completed", merged
    assert "# second round" in (repo / "calc.py").read_text(encoding="utf-8")
    hub.close()


def test_same_blocker_twice_blocks_early(tmp_path):
    blocker = ["修复 calc.py 的 add 并运行 unittest"]
    scenario = {
        "tasks": {**_base_tasks(), "R2_T001": _round2_task("still failing")},
        "summary_sequence": [_reject(blocker), _reject(blocker)],
    }
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    hub.config.round_replan = False
    hub.config.blocked_after_rounds = 2
    run_id = _run_plan(hub)
    result = hub.start_run(run_id, max_parallel=2)
    assert result["status"] == "waiting_user", result
    run = hub.store.get_run(run_id)
    assert run["goal_status"] == "blocked", run
    assert "repeated-blocker" in run["blocked_reason"], run
    assert int(run["round_number"]) == 2, run
    assert not hub.approvals_pending(), "阻塞轮不应创建验收审批"
    hub.close()


def test_round_limit_blocks_without_acceptance(tmp_path):
    scenario = {
        "tasks": {**_base_tasks(), "R2_T001": _round2_task("round two")},
        "summary_sequence": [
            _reject(["修复 calc.py 的 add"]),
            _reject(["补充另一处验证证据"], completion="仍有验证缺口"),
        ],
    }
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    hub.config.round_replan = False
    hub.config.max_rounds = 2
    hub.config.blocked_after_rounds = 5
    run_id = _run_plan(hub)
    result = hub.start_run(run_id, max_parallel=2)
    assert result["status"] == "waiting_user", result
    run = hub.store.get_run(run_id)
    assert run["goal_status"] == "blocked", run
    assert "round-limit" in run["blocked_reason"], run
    assert not hub.approvals_pending()
    hub.close()


def test_main_agent_delta_plan_is_used_for_next_round(tmp_path):
    round_plan = {
        "plan_version": 1,
        "goal": "fix add",
        "constraints": [],
        "tasks": [{
            "task_id": "D1", "title": "delta fix", "type": "implementation",
            "objective": "根据上一轮未解决项修复 calc.py", "scope": ["calc.py"],
            "out_of_scope": [], "dependencies": [], "expected_outputs": ["code_patch"],
            "acceptance_criteria": ["tests pass"], "verification_commands": ["python -m unittest -q"],
            "token_budget": 10000, "time_limit_minutes": 10, "retry_limit": 1,
            "risk_level": "low", "priority": 8,
        }],
    }
    scenario = {
        "tasks": {**_base_tasks(), "R2_D1": _round2_task("delta round")},
        "summary_sequence": [
            _reject(["修复 calc.py 的 add 并运行 unittest"]),
            _approve(),
        ],
        "round_plan": round_plan,
    }
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    hub.config.round_replan = True
    run_id = _run_plan(hub)
    result = hub.start_run(run_id, max_parallel=2)
    assert result["status"] == "pending_acceptance", result
    task_ids = {t["task_id"] for t in hub.tasks(run_id=run_id)}
    assert "R2_D1" in task_ids, task_ids
    planned = next(e for e in hub.audit_events() if e["action"] == "round.plan_appended")
    assert planned["payload"]["source"] == "main_agent", planned
    assert "R2_D1" in planned["payload"]["tasks"], planned
    hub.close()


def test_repair_round_can_complete_after_first_round_task_failure(tmp_path):
    tasks = _base_tasks()
    tasks["T003"] = {"status": "failed", "summary": "first round failed"}
    scenario = {
        "tasks": {**tasks, "R2_T001": _round2_task("repair")},
        "summary_sequence": [
            _reject(["修复 calc.py 的 add 并运行 unittest"]),
            _approve(),
        ],
    }
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    hub.config.round_replan = False
    run_id = _run_plan(hub)
    result = hub.start_run(run_id, max_parallel=2)
    assert result["status"] == "pending_acceptance", result
    statuses = {t["task_id"]: t["status"] for t in hub.tasks(run_id=run_id)}
    assert statuses["T003"] == "failed", statuses
    assert statuses["R2_T001"] == "completed", statuses
    assert hub.store.get_run(run_id)["round_number"] == 2
    hub.close()


def test_missing_summary_continues_from_task_evidence(tmp_path):
    tasks = _base_tasks()
    tasks["T003"] = {"status": "failed", "summary": "first round failed"}
    scenario = {
        "tasks": {**tasks, "R2_T001": _round2_task("repaired without summary")},
        "summary_missing": True,
    }
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    hub.config.round_replan = False
    hub.config.max_rounds = 2
    run_id = _run_plan(hub)
    result = hub.start_run(run_id, max_parallel=2)
    assert result["status"] == "waiting_user", result
    run = hub.store.get_run(run_id)
    assert "summary-unavailable" in run["blocked_reason"], run
    statuses = {t["task_id"]: t["status"] for t in hub.tasks(run_id=run_id)}
    assert statuses["R2_T001"] == "completed", statuses
    actions = [e["action"] for e in hub.audit_events()]
    assert "summary.fallback_continue" in actions, actions
    assert int(run["round_number"]) == 2, run
    hub.close()


def test_queued_dependents_become_blocked_before_finalize(tmp_path):
    plan = {
        "plan_version": 1, "goal": "failure chain", "constraints": [],
        "tasks": [
            {"task_id": "A", "title": "a", "type": "analysis", "objective": "a",
             "dependencies": [], "scope": [], "out_of_scope": [], "expected_outputs": [],
             "acceptance_criteria": [], "verification_commands": [], "token_budget": 1000,
             "time_limit_minutes": 5, "retry_limit": 0, "risk_level": "low"},
            {"task_id": "B", "title": "b", "type": "analysis", "objective": "b",
             "dependencies": ["A"], "scope": [], "out_of_scope": [], "expected_outputs": [],
             "acceptance_criteria": [], "verification_commands": [], "token_budget": 1000,
             "time_limit_minutes": 5, "retry_limit": 0, "risk_level": "low"},
            {"task_id": "C", "title": "c", "type": "analysis", "objective": "c",
             "dependencies": ["B"], "scope": [], "out_of_scope": [], "expected_outputs": [],
             "acceptance_criteria": [], "verification_commands": [], "token_budget": 1000,
             "time_limit_minutes": 5, "retry_limit": 0, "risk_level": "low"},
        ],
    }
    scenario = {"plan": plan, "tasks": {"A": {"result": {"status": "failed"},
                                                "summary": "failed"}}}
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    hub.config.multi_round = False
    imported = hub.import_plan(plan)
    hub.decide_approval(imported["approval_id"], approve=True)
    run_id = hub.latest_run()["run_id"]
    result = hub.start_run(run_id, max_parallel=1)
    assert result["status"] == "failed", result
    statuses = {t["task_id"]: t["status"] for t in hub.tasks(run_id=run_id)}
    assert statuses == {"A": "failed", "B": "blocked", "C": "blocked"}, statuses
    hub.close()


def test_multi_round_setting_persists(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    settings = hub.settings()
    assert settings["multi_round"] is True
    assert settings["max_rounds"] == 3
    saved = hub.set_multi_round(False, max_rounds=2)
    assert saved["multi_round"] is False and saved["max_rounds"] == 2
    data = json.loads((repo / ".agentloop" / "config.json").read_text(encoding="utf-8"))
    assert data["multi_round"] is False
    assert data["max_rounds"] == 2
    assert hub.settings()["multi_round"] is False
    hub.close()


def _task(*, status="completed", verification="passed", error="", task_id="T1"):
    spec = TaskSpec(task_id=task_id, title="t", objective="o", type="analysis")
    return Task(
        uid=f"run:{task_id}", project_id="p", plan_id="plan", run_id="run",
        spec=spec, status=status, verification_status=verification, error=error,
    )


def test_approve_cannot_override_failed_verification():
    decision = decide(
        tasks=[_task(verification="failed")], summary=_approve(),
        round_number=1, max_rounds=3, blocked_after_rounds=2,
    )
    assert decision.action == "continue", decision
    assert decision.goal_status == "active"


def test_identical_blocker_signature_blocks_after_threshold():
    blocker = ["修复 calc.py 的 add"]
    first = decide(
        tasks=[_task()], summary=_reject(blocker),
        round_number=1, max_rounds=5, blocked_after_rounds=2,
    )
    assert first.action == "continue", first
    assert first.signature == signature_of(first.unresolved)
    second = decide(
        tasks=[_task()], summary=_reject(blocker),
        round_number=2, max_rounds=5, blocked_after_rounds=2,
        history=[first.to_dict()],
    )
    assert second.action == "blocked", second
    assert second.code == "repeated-blocker"
    assert second.blocked_streak == 2



def test_collect_unresolved_ignores_nonblocking_notes():
    from types import SimpleNamespace
    from agentloop_hub.control.rounds import collect_unresolved

    task = SimpleNamespace(uid="run:T1", task_id="T1", status="completed",
                           verification_status="warning", error="",
                           spec=SimpleNamespace(supersedes=[]))
    results = {"run:T1": {"unresolved_items": [
        "非阻塞：浏览器人工复核留待验收",
        "optional: document later",
        "真正阻塞：登录流程仍然失败",
    ]}}
    items = collect_unresolved([task], {"merge_recommendation": "approve"}, results)
    assert len(items) == 1
    assert "登录流程" in items[0]


def test_fallback_specs_restate_constraints_and_delivery_rules():
    from agentloop_hub.control.rounds import (
        DELIVERY_RULES, RoundDecision, fallback_specs,
    )
    from agentloop_hub.models import TaskPlan

    plan = TaskPlan(plan_id="p", project_id="prj", goal="g", tasks=[],
                    main_agent_id="main", model="m",
                    constraints=["禁止 git 提交", "只改 src/"])
    decision = RoundDecision(action="continue", reason="未通过",
                             unresolved=["清理 6 个临时文件：git rm --cached 并提交"])
    specs = fallback_specs(plan=plan, tasks=[], summary={}, decision=decision,
                           round_number=2)
    objective = specs[0].objective
    # Constraints and the real delivery mechanism are restated verbatim.
    assert "禁止 git 提交" in objective
    assert "只改 src/" in objective
    for rule in DELIVERY_RULES:
        assert rule in objective
    assert "git rm --cached" in objective
    # The recorded unresolved item is preserved as evidence, not rewritten.
    assert "git rm --cached 并提交" in objective
    assert specs[1].task_id.endswith("_REVIEW")


def test_continuation_context_carries_plan_constraints(tmp_path):
    from types import SimpleNamespace
    from agentloop_hub.control.rounds import RoundDecision
    from agentloop_hub.models import TaskPlan

    _, hub, _ = make_project(tmp_path)
    plan = TaskPlan(plan_id="p", project_id="prj", goal="g", tasks=[],
                    main_agent_id="main", model="m",
                    constraints=["禁止 git 提交"])
    decision = RoundDecision(action="continue", reason="r", unresolved=["x"])
    context = hub.runner._continuation_context(
        {"round_number": 1, "max_rounds": 3}, plan, [], {},
        decision, 2, 3)
    assert context["plan_constraints"] == ["禁止 git 提交"]
    assert context["delivery_rules"]
    assert any("git rm --cached" in rule for rule in context["delivery_rules"])
    hub.close()
