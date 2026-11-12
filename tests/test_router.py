from __future__ import annotations

from hub_test_support import default_plan, make_project


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


def test_capability_score_is_task_type_aware(tmp_path):
    from agentloop_hub.models import TaskSpec

    repo, hub, _ = make_project(tmp_path)
    router = hub.router
    agent = hub.get_agent("mock_worker")
    impl = TaskSpec(task_id="I", title="i", objective="o", type="implementation")
    review = TaskSpec(task_id="R", title="r", objective="o", type="review")
    history = {"tasks_total": 5, "completed": 5, "failed": 0}
    impl_cap, impl_detail = router._capability_score(agent, impl, history)
    review_cap, _ = router._capability_score(agent, review, {})
    # Measured success on implementation tasks lifts fitness above the neutral
    # prior used for an untested type — capability is no longer a constant.
    assert impl_cap > review_cap
    assert impl_detail["type_tasks_total"] == 5
    assert impl_detail["fitness"] > hub.config.router_untested_prior
    hub.close()


def test_capability_specialization_rewards_explicit_declaration(tmp_path):
    from agentloop_hub.models import TaskSpec

    repo, hub, _ = make_project(tmp_path)
    router = hub.router
    spec = TaskSpec(task_id="I", title="i", objective="o", type="implementation")
    derived_agent = hub.get_agent("mock_worker")
    _, derived_detail = router._capability_score(derived_agent, spec, {})
    explicit_agent = hub.get_agent("mock_worker")
    explicit_agent.capabilities = {**(explicit_agent.capabilities or {}), "code_generation": True}
    _, explicit_detail = router._capability_score(explicit_agent, spec, {})
    assert explicit_detail["specialization"] == 1.0
    assert derived_detail["specialization"] < explicit_detail["specialization"]
    hub.close()


def test_routing_breakdown_exposes_capability_profile(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    result = hub.import_plan(default_plan(str(tmp_path)))
    hub.decide_approval(result["approval_id"], approve=True)
    run_id = result.get("run_id") or hub.latest_run()["run_id"]
    task = next(t for t in hub.store.list_tasks(run_id) if t.task_id == "T003")
    decision = hub.router.select(task)
    assert decision is not None
    breakdown = decision.breakdown
    assert "capability_specialization" in breakdown
    assert "capability_type_fitness" in breakdown
    assert "type_tasks_total" in breakdown
    assert 0.0 <= breakdown["capability"] <= 1.0
    hub.close()
