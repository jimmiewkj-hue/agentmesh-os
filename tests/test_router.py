from __future__ import annotations

from conftest import default_plan, make_project


def test_route_preview_respects_capabilities_and_excludes_main(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    preview = hub.route_preview(task_type="implementation")
    assert preview["selected"] is not None
    assert "write_files" in preview["required_capabilities"]
    assert preview["selected"]["agent_id"] != "mock_main"
    rejected = {c["agent_id"]: c for c in preview["candidates"] if c.get("rejected")}
    assert "mock_main" in rejected
    assert "主节点" in " ".join(rejected["mock_main"]["rejected"])


def test_route_task_records_decision(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    result = hub.import_plan(default_plan(str(tmp_path)))
    hub.decide_approval(result["approval_id"], approve=True)
    run_id = result.get("run_id") or hub.latest_run()["run_id"]
    task = next(t for t in hub.store.list_tasks(run_id) if t.task_id == "T003")
    decision = hub.router.select(task)
    assert decision is not None
    assert decision.agent.agent_id != "mock_main"
    recorded = hub.store.latest_routing(task.uid)
    assert recorded and recorded["agent_id"] == decision.agent.agent_id
    assert recorded["breakdown"]["success"] >= 0
    ranking = hub.route_task(task.uid)
    assert ranking["selected"]["agent_id"] == decision.agent.agent_id
    hub.close()


def test_router_rejects_missing_capability(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    hub.register_agent({
        "agent_id": "read_only", "name": "Read Only", "product": "Read Only",
        "adapter_type": "cli", "access_level": "L3",
        "adapter_config": {"command": "fake"},
        "capabilities": {"read_files": True, "write_files": False, "terminal": False,
                         "structured_output": True},
    })
    preview = hub.route_preview(task_type="implementation", required=["code_generation"])
    assert preview["selected"] is not None
    assert preview["selected"]["agent_id"] != "read_only"
    read_preview = hub.route_preview(task_type="analysis")
    assert read_preview["selected"] is not None
    hub.close()


def test_router_rejects_manual_relay_agents(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    hub.register_agent({
        "agent_id": "manual_relay", "name": "Manual Relay", "product": "Manual Relay",
        "adapter_type": "file_relay", "access_level": "L3",
        "adapter_config": {"mode": "file_relay"},
        "capabilities": {"read_files": True, "write_files": True, "terminal": True,
                         "structured_output": True},
    })
    preview = hub.route_preview(task_type="implementation")
    rejected = {c["agent_id"]: c for c in preview["candidates"] if c.get("rejected")}
    assert "manual_relay" in rejected
    assert "不可自动执行" in " ".join(rejected["manual_relay"]["rejected"])

    # Explicit opt-in makes the relay Agent eligible again.
    hub.config.router_allow_manual_relay = True
    hub.router.config = hub.config
    preview2 = hub.route_preview(task_type="implementation")
    assert any(c.get("agent_id") == "manual_relay" for c in preview2["candidates"])
    hub.close()


def test_untested_agent_gets_exploration_penalty(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    router = hub.router
    assert router._success_score({}) == hub.config.router_untested_prior
    one_failure = router._success_score({"tasks_total": 1, "completed": 0})
    assert one_failure >= hub.config.router_untested_prior
    assert router._verification_score({}) == hub.config.router_untested_prior
    hub.close()


def test_router_scores_include_cost_and_speed(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    scores = hub.router_scores()
    assert scores["agents"]
    assert "weights" in scores
    assert any(item["stats"].get("tasks_total", 0) >= 0 for item in scores["agents"])
    hub.close()
