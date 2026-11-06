"""End-to-end ``solve``: one request, several models, one aggregated answer.

These tests run real subprocesses (the mock CLI agent) through the same
file contract the real adapters use, so what they verify is the controller:
segmentation, role resolution, per-task model pinning, escalation on failure,
premium budgeting, and aggregation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hub_test_support import make_project, write_scenario

from agentloop_hub.hub import Hub


@pytest.fixture()
def env(tmp_path: Path):
    repo, hub, scenario_path = make_project(tmp_path)
    # Distinct model names per agent so per-task routing is observable, and a
    # clean catalog (register_agent replaces the model list).
    for agent_id, models in (
        ("mock_main", ["main-pro"]),
        ("mock_worker", ["worker-free"]),
        ("mock_verifier", ["verifier-pro"]),
    ):
        record = hub.get_agent(agent_id)
        hub.register_agent({
            "agent_id": agent_id, "name": record.name, "product": record.product,
            "adapter_type": record.adapter_type, "access_level": record.access_level,
            "adapter_config": record.adapter_config,
            "capabilities": record.capabilities, "models": models,
            "status": record.status,
        })
    return repo, hub, scenario_path


def solve_scenario() -> dict:
    return {
        "tasks": {
            "S01": {
                "write_files": [{"path": "calc.py",
                                 "content": "def add(a, b):\n    return a + b\n"}],
                "summary": "把 add() 的减法改成加法",
                "tests": [{"command": "python -m unittest -q", "result": "passed"}],
                "evidence": [{"file": "calc.py", "lines": "1-2",
                              "description": "return a + b"}],
            },
            "S02": {
                "summary": "复跑单测通过，改动只在 calc.py",
                "tests": [{"command": "python -m unittest -q", "result": "passed"}],
                "evidence": [{"file": "calc.py", "lines": "1-2",
                              "description": "verified"}],
            },
        },
        "verification": {"checks": {"scope_check": "passed", "unit_tests": "passed"}},
    }


def test_solve_segments_pins_models_runs_and_aggregates(env):
    repo, hub, scenario_path = env
    write_scenario(scenario_path, solve_scenario())

    out = hub.solve("修复 calc.py 的加法错误并补充测试", max_parallel=1,
                    start=True, background=False, approve=True)
    assert out["run_id"]
    assert [b["role"] for b in out["bindings"]] == ["implement", "verify"]

    run_id = out["run_id"]
    tasks = {t["task_id"]: t for t in hub.tasks(run_id=run_id)}
    assert set(tasks) == {"S01", "S02"}
    assert tasks["S01"]["assigned_agent"] == "mock_worker"
    assert tasks["S01"]["spec"]["model"] == "worker-free"
    assert tasks["S01"]["role"] == "implement"
    assert tasks["S01"]["status"] == "completed"
    # verification is never pinned to the agent that did the work
    assert tasks["S02"]["assigned_agent"] != tasks["S01"]["assigned_agent"]
    assert tasks["S02"]["spec"]["model"] != tasks["S01"]["spec"]["model"]

    # The model the adapter actually dispatched is what the agent saw.
    for task in tasks.values():
        marker = Path(task["task_dir"]) / "dispatched-model.txt"
        assert marker.read_text(encoding="utf-8") == task["spec"]["model"]

    status = hub.run_status(run_id)
    assert status["run"]["status"] in ("pending_acceptance", "completed")

    answer = hub.aggregate_run(run_id)
    assert [s["task_id"] for s in answer["sections"]] == ["S01", "S02"]
    assert "def add" not in answer["answer_markdown"]  # digests, not raw content
    assert "calc.py" in answer["files_changed"]
    assert answer["cost"]["total_tokens"] > 0
    assert Path(hub.paths.run_dir(run_id) / "answer.md").exists()

    # A single request produced a plan without any LLM planner call, and the
    # execution stage stayed on the free tier.
    assert out["confidence"] >= 0.75
    by_role = {b["role"]: b for b in out["bindings"]}
    assert by_role["implement"]["tier"] == 1
    assert out["premium_tasks"] <= 1


def test_solve_escalates_a_failed_task_to_the_next_ladder_rung(env):
    repo, hub, scenario_path = env
    scenario = solve_scenario()
    scenario["tasks"]["S01"]["fail_once_marker"] = "s01.failed"
    write_scenario(scenario_path, scenario)

    out = hub.solve("修复 calc.py 的加法错误并补充测试", start=True, background=False, approve=True)
    run_id = out["run_id"]
    task = hub.tasks(run_id=run_id)[0]
    assert task["task_id"] == "S01"
    assert task["status"] == "completed", task["error"]
    assert task["attempts"] == 2
    # worker-free failed, so the retry moved to the next rung (a stronger model).
    assert task["assigned_agent"] != "mock_worker"
    assert task["spec"]["model"] != "worker-free"
    escalations = [e for e in hub.audit_events(limit=500)
                   if e["action"] == "task.escalated"]
    assert escalations and escalations[-1]["payload"]["role"] == "implement"


def test_premium_budget_shifts_the_second_decision_task_to_a_cheaper_rung(tmp_path: Path):
    repo, hub, scenario_path = make_project(tmp_path)
    hub.register_agent_models("mock_main", ["main-pro"])
    hub.register_agent_models("mock_worker", ["worker-pro", "worker-std"])
    policy = {
        "name": "budget-test",
        "premium_tier": 4,
        "max_premium_tasks_per_run": 1,
        "roles": {
            "plan": {"task_type": "architecture", "write": False,
                     "ladder": [{"tier": 4, "agents": ["mock_main"],
                                 "models": ["main-pro"]}]},
            "implement": {"task_type": "implementation", "write": True,
                          "ladder": [{"tier": 4, "agents": ["mock_worker"],
                                      "models": ["worker-pro"]},
                                     {"tier": 3, "agents": ["mock_worker"],
                                      "models": ["worker-std"]}]},
        },
    }
    policy_dir = repo / ".agentloop"
    policy_dir.mkdir(exist_ok=True)
    (policy_dir / "roles.json").write_text(json.dumps(policy), encoding="utf-8")

    hub = Hub(repo, auto_discover=False)  # reloads the project policy
    assert hub.roles.name == "budget-test"

    plan = hub.segment_goal("设计一个缓存层并实现")
    assert [b["task"]["role"] for b in plan["bindings"]] == ["plan", "implement"]

    out = hub.solve("设计一个缓存层并实现", start=False)
    assert out["premium_tasks"] == 1
    bindings = {b["role"]: b for b in out["bindings"]}
    assert bindings["plan"]["model"] == "main-pro"
    assert bindings["plan"]["tier"] == 4
    # the premium budget was already spent by the plan stage
    assert bindings["implement"]["model"] == "worker-std"
    assert bindings["implement"]["tier"] == 3


def test_solve_rejects_a_pin_to_a_model_the_agent_does_not_have(env):
    from agentloop_hub.errors import ValidationError

    repo, hub, _ = env
    with pytest.raises(ValidationError) as exc:
        hub.solve("修复 calc.py", executor_agent="mock_worker",
                  executor_model="kimi-k3.1-pro", start=False)
    assert "真实模型清单" in str(exc.value)


def test_solve_with_no_start_creates_a_plan_but_does_not_run(env):
    repo, hub, scenario_path = env
    write_scenario(scenario_path, solve_scenario())
    out = hub.solve("修复 calc.py 的加法错误并补充测试", start=False)
    assert out["status"] == "pending_approval"
    assert out["run_id"] == ""
    assert hub.runs() == []
    approved = hub.decide_approval(out["approval_id"], approve=True)
    run_id = approved["outcome"]["run_id"]
    run = hub.run_status(run_id)
    assert {t["status"] for t in run["tasks"]} == {"queued"}


def test_solve_status_returns_the_answer_only_when_the_run_is_settled(env):
    repo, hub, scenario_path = env
    write_scenario(scenario_path, solve_scenario())
    out = hub.solve("修复 calc.py 的加法错误并补充测试", start=False)
    approved = hub.decide_approval(out["approval_id"], approve=True)
    run_id = approved["outcome"]["run_id"]
    assert hub.solve_status(run_id)["answer"] is None

    hub.start_run(run_id, max_parallel=1, background=False)
    status = hub.solve_status(run_id)
    assert status["answer"] is not None
    assert status["answer"]["run_id"] == run_id
    assert {t["role"] for t in status["tasks"]} == {"implement", "verify"}


def test_solve_picks_a_main_node_when_none_is_configured(env):
    repo, hub, scenario_path = env
    write_scenario(scenario_path, solve_scenario())
    hub.db.execute("DELETE FROM main_nodes")
    assert hub.get_main() is None
    out = hub.solve("修复 calc.py 的加法错误并补充测试", start=False)
    assert out["status"] == "pending_approval"
    assert hub.get_main() is not None
    assert any("自动选择主节点" in note for note in out["notes"])


def test_chain_still_pins_models_per_stage_and_reports_them(env):
    """The chain path shares the new per-task model column."""
    repo, hub, scenario_path = env
    write_scenario(scenario_path, {
        "tasks": {"CHAIN01": {"summary": "scan"},
                  "CHAIN02": {"summary": "review"}},
    })
    out = hub.configure_chain(
        goal="scan then review",
        stages=[{"agent_id": "mock_worker", "model": "worker-free", "type": "analysis"},
                {"agent_id": "mock_verifier", "model": "verifier-pro", "type": "review"}],
        start=True, background=False, max_parallel=1,
    )
    status = hub.chain_status(out["run_id"])
    assert [s["model"] for s in status["stages"]] == ["worker-free", "verifier-pro"]
