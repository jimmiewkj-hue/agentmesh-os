"""Agent/model profile and in-run node binding."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_project

from agentloop_hub.models import TaskSpec


@pytest.fixture()
def env(tmp_path: Path):
    repo, hub, scenario_path = make_project(tmp_path)
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


def test_agent_model_profile_is_persisted_and_exposed(env):
    repo, hub, _ = env
    hub.set_agent_model("mock_worker", "worker-free",
                        context_length=32000, capability_level="high")
    catalog = hub.agent_models()
    entry = catalog["by_agent"]["mock_worker"]
    assert entry["context_length"] == 32000
    assert entry["capability_level"] == "high"
    assert entry["model_profiles"]["worker-free"]["context_length"] == 32000
    row = next(m for m in entry["models"] if m["name"] == "worker-free")
    assert row["capability_level"] == "high"

    hub.set_main("mock_main", "main-pro", context_length=64000,
                 capability_level="ultra")
    main = hub.get_main()
    assert main["context_length"] == 64000
    assert main["capability_level"] == "ultra"


def test_taskspec_profile_roundtrip_and_validation():
    spec = TaskSpec(task_id="T1", title="x", objective="x", type="analysis",
                    scope=[], context_length=16000, capability_level="low")
    spec.validate()
    payload = spec.to_dict()
    restored = TaskSpec.from_dict(payload)
    assert restored.context_length == 16000
    assert restored.capability_level == "low"
    bad = TaskSpec(task_id="T2", title="x", objective="x", capability_level="god")
    with pytest.raises(Exception):
        bad.validate()


def test_task_binding_can_be_edited_during_run(env):
    repo, hub, _ = env
    imported = hub.import_plan(
        {"plan_version": 1, "goal": "binding", "constraints": [],
         "tasks": [{"task_id": "T1", "title": "scan", "type": "analysis",
                    "objective": "scan", "suggested_agent": "mock_worker",
                    "scope": [], "dependencies": []}]},
        main_agent_id="mock_main", model="main-pro",
    )
    for approval in hub.approvals_pending():
        if approval["kind"] == "architecture":
            hub.decide_approval(approval["approval_id"], approve=True)
    run_id = hub.approve_plan(imported["plan_id"])["run_id"]
    uid = f"{run_id}:T1"
    updated = hub.update_task_binding(
        uid, agent_id="mock_verifier", model="verifier-pro",
        context_length=12000, capability_level="medium",
    )
    assert updated["assigned_agent"] == "mock_verifier"
    assert updated["spec"]["model"] == "verifier-pro"
    assert updated["spec"]["context_length"] == 12000
    assert updated["spec"]["capability_level"] == "medium"
    task = hub.task(uid)
    profile = hub.runner._agent_profile(
        hub.get_agent("mock_verifier"), task, "verifier-pro")
    assert profile == {"context_length": 12000, "capability_level": "medium"}
    with pytest.raises(Exception):
        hub.update_task_binding(uid, capability_level="god")
