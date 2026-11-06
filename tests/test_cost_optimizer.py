from __future__ import annotations

import json
from pathlib import Path

from hub_test_support import default_plan, make_project


def simple_plan(goal: str, objective: str) -> dict:
    return {
        "plan_version": 1, "goal": goal, "constraints": [],
        "tasks": [{
            "task_id": "A1", "title": "scan calc", "type": "analysis",
            "objective": objective, "suggested_agent": "mock_worker",
            "dependencies": [], "scope": ["calc.py"], "out_of_scope": [],
            "expected_outputs": ["summary"], "acceptance_criteria": ["report"],
            "verification_commands": [], "token_budget": 5000,
            "time_limit_minutes": 5, "retry_limit": 0, "risk_level": "low",
            "priority": 5,
        }],
    }


def scenario_for(plan: dict, objective: str) -> dict:
    return {
        "plan": plan,
        "tasks": {"A1": {
            "summary": objective, "findings": [{"severity": "low", "summary": objective}],
            "evidence": [{"file": "calc.py", "lines": "1-2", "description": "read"}],
        }},
        "summary": {"summary_version": 1, "completion": "scan done", "changes": [],
                    "verification": "passed", "risks": [],
                    "merge_recommendation": "approve", "next_actions": []},
    }


def test_similar_analysis_is_a_reference_not_a_cached_result(tmp_path):
    objective1 = "检查 calc.py 的加法实现并总结风险"
    objective2 = "排查 calc.py 的加法实现并总结风险"
    plan1 = simple_plan("scan one", objective1)
    plan2 = simple_plan("scan two", objective2)
    scenario = scenario_for(plan1, objective1)
    repo, hub, scenario_path = make_project(tmp_path, scenario=scenario)
    scenario = scenario_for(plan2, objective2)
    scenario_path.write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")

    run_ids = []
    for label in ("first", "second"):
        result = hub.import_plan(plan1 if label == "first" else plan2)
        hub.decide_approval(result["approval_id"], approve=True)
        run_id = hub.latest_run()["run_id"]
        outcome = hub.start_run(run_id, max_parallel=1)
        assert outcome["status"] == "pending_acceptance", outcome
        run_ids.append(run_id)

    hits = [e for e in hub.audit_events() if e["action"] == "task.cache_hit"]
    # A paraphrased objective is not proof of the same intent; it must not
    # silently reuse a prior result or count as a saving.
    assert not any(h["payload"].get("source") == "semantic_cache" for h in hits)
    savings = hub.cost_report()["savings"]
    assert savings["semantic_cache"]["hits"] == 0
    assert savings["semantic_cache"]["entries"] >= 1
    hub.close()


def test_benchmark_and_cheapest_sufficient(tmp_path):
    objective = "检查 calc.py 的加法实现并总结风险"
    plan = simple_plan("scan", objective)
    repo, hub, _ = make_project(tmp_path, scenario=scenario_for(plan, objective))
    result = hub.import_plan(plan)
    hub.decide_approval(result["approval_id"], approve=True)
    run_id = hub.latest_run()["run_id"]
    hub.start_run(run_id, max_parallel=1)
    report = hub.benchmark()
    assert report["agents"]
    assert "analysis" in report["cheapest_sufficient"]
    recommended = report["cheapest_sufficient"]["analysis"]["recommended"]
    assert recommended
    latest = hub.store.latest_benchmarks(hub.project.project_id)
    assert latest
    hub.close()


def test_context_compression_records_savings(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    big = {"items": [{"text": "x" * 200} for _ in range(200)], "keep": "id"}
    compressed = hub.cost.compress_context(big, 500, kind="context_compression")
    assert compressed.get("_truncated") is True
    savings = hub.cost_report()["savings"]
    kinds = {row["kind"]: row for row in savings["by_kind"]}
    assert "context_compression" in kinds
    assert int(kinds["context_compression"]["tokens_saved"]) > 0
    hub.close()


def test_upstream_result_digest_is_injected_and_saved(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    plan = default_plan(str(tmp_path))
    result = hub.import_plan(plan)
    hub.decide_approval(result["approval_id"], approve=True)
    run_id = hub.latest_run()["run_id"]
    outcome = hub.start_run(run_id, max_parallel=2)
    assert outcome["status"] == "pending_acceptance", outcome
    task3 = next(t for t in hub.store.list_tasks(run_id) if t.task_id == "T003")
    upstream = hub.context.upstream_results(task3)
    assert upstream and upstream[0]["task_id"] == "T001"
    assert "summary" in upstream[0] and "digest" not in upstream[0]
    package = json.loads((Path(task3.task_dir) / "task.json").read_text(encoding="utf-8"))
    assert package["extra_context"]["upstream_results"]
    savings = hub.cost_report()["savings"]
    kinds = {row["kind"]: row for row in savings["by_kind"]}
    assert "intermediate_reuse" in kinds
    hub.close()
