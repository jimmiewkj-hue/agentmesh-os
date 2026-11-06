"""S10: capability and cost claims must match evidence."""

from __future__ import annotations

from pathlib import Path

from hub_test_support import default_plan, default_scenario, make_project

from agentloop_hub.adapters.base import ExecutionUsage


def test_unpriced_model_is_unknown_not_free(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path)
    result = hub.import_plan(default_plan(str(repo)))
    approved = hub.decide_approval(result["approval_id"], approve=True)
    task = hub.store.list_tasks(approved["outcome"]["run_id"])[0]
    record = hub.budget.record(
        task, "mock_worker", "unpriced/model",
        ExecutionUsage(input_tokens=1000, output_tokens=100, total_tokens=1100),
        duration_ms=10, run_id=task.run_id,
    )
    assert record.cost == 0.0
    assert record.cost_known is False
    assert record.status == "unknown_cost"

    credits = hub.budget.record(
        task, "mock_worker", "credits/model",
        ExecutionUsage(cost=5.0, currency="credits", cost_known=True,
                       total_tokens=10),
        duration_ms=5, run_id=task.run_id,
    )
    assert credits.currency == "credits"
    totals = hub.store.usage_totals(hub.project.project_id)
    # USD and credits are never summed into one number.
    assert float(totals["cost"]) == 0.0
    assert float(totals["credits"]) == 5.0
    assert int(totals["unknown_cost_records"]) >= 1
    currencies = {row["currency"]: row for row in totals["cost_by_currency"]}
    assert "credits" in currencies
    hub.close()


def test_pinned_model_missing_from_catalog_blocks_instead_of_switching(tmp_path: Path):
    scenario = default_scenario()
    scenario["plan"] = {
        "plan_version": 1, "goal": "pin gate", "constraints": [],
        "tasks": [{
            "task_id": "P1", "title": "pinned", "type": "analysis",
            "objective": "scan", "suggested_agent": "mock_worker",
            "model": "not-a-real-model", "dependencies": [], "scope": [],
            "out_of_scope": [], "verification_commands": [], "token_budget": 5000,
            "time_limit_minutes": 5, "retry_limit": 0, "risk_level": "low",
        }],
    }
    scenario["tasks"] = {"P1": {"summary": "should not run"}}
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    requested = hub.request_plan("pin gate", main_agent_id="mock_main",
                                 model="mock-model")
    approved = hub.decide_approval(requested["approval_id"], approve=True)
    run_id = approved["outcome"]["run_id"]
    hub.start_run(run_id, max_parallel=1)
    task = next(t for t in hub.tasks(run_id=run_id) if t["task_id"] == "P1")
    assert task["status"] == "blocked", task
    assert "真实模型清单" in (task.get("error") or "")
    hub.close()


def test_capability_evidence_separates_declared_disabled_and_probed(tmp_path: Path):
    from agentloop_hub.connectors import capability_evidence
    from agentloop_hub.models import AgentRecord

    disabled = AgentRecord(
        agent_id="disabled", name="Disabled", product="x", adapter_type="cli",
        status="online", version="1.0", adapter_config={"command": ["x"]},
        capabilities={"read_files": True, "write_files": False,
                      "auto_executable": False},
        models=[], access_level="L3",
    )
    evidence = capability_evidence(disabled)
    assert evidence["write_files"]["status"] == "disabled"
    assert evidence["auto_executable"]["status"] == "unavailable"

    probed = AgentRecord(
        agent_id="cli", name="CLI", product="claude_code", adapter_type="cli",
        status="online", version="2.1", adapter_config={"command": ["claude"]},
        capabilities={}, models=[], access_level="L3",
    )
    evidence = capability_evidence(probed)
    assert evidence["auto_executable"]["status"] == "probed"
    assert evidence["auto_executable"]["version"] == "2.1"

    offline = AgentRecord(
        agent_id="offline", name="Offline", product="claude_code", adapter_type="cli",
        status="offline", version="1", adapter_config={"command": ["claude"]},
        capabilities={}, models=[], access_level="L3",
    )
    assert capability_evidence(offline)["auto_executable"]["status"] == "unavailable"
