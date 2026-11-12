"""Heavy-task free-tier avoidance (2026-11-07 defect #6).

A free-tier model burned 2x120 minutes on a Blender/GLB rebuild that a
standard model finished in 51-85 minutes.  Heavy work is recognised from the
task's own words/commands; free tiers are skipped by the role ladder and the
model recommender, and a WebUI chain pin of a free model on a heavy stage
returns a visible warning (the user's pin is never silently rewritten).
"""
from __future__ import annotations

import pytest

from agentloop_hub.control.roles import (
    RolePolicy, classify_model, is_heavy_task, spec_is_heavy,
)
from agentloop_hub.models import TaskSpec
from hub_test_support import make_project, mock_agent_config


def test_heavy_task_detection():
    assert is_heavy_task("全链路重建+实机验收")
    assert is_heavy_task("重建 buildings.glb，运行 Blender 导出")
    assert is_heavy_task("五区三维布局与 draw call 验收")
    assert not is_heavy_task("修复 README 拼写")
    assert not is_heavy_task("rename function to snake_case")
    spec = TaskSpec(task_id="T1", title="模型", objective="Blender 重建",
                    type="implementation", scope=["tools/blender/**"])
    assert spec_is_heavy(spec)
    light = TaskSpec(task_id="T2", title="文档", objective="补充 README",
                     type="documentation", scope=["README.md"])
    assert not spec_is_heavy(light)
    assert classify_model("opencode/glm-5.3-flash-free") == "free"


def _implement_agents(hub, tmp_path):
    from agentloop_hub.models import AgentRecord
    agent = AgentRecord(
        agent_id="heavy_worker", name="Heavy Worker", product="mock",
        adapter_type="cli", access_level="L3", status="online",
        adapter_config=mock_agent_config(tmp_path / "none.json", "heavy_worker"),
        capabilities={"read_files": True, "write_files": True, "terminal": True,
                      "structured_output": True},
        models=["opencode/glm-5.3-flash-free", "standard-model"],
    )
    hub.store.upsert_agent(agent)
    hub.store.set_agent_models("heavy_worker",
                               ["opencode/glm-5.3-flash-free", "standard-model"])
    return agent


def test_role_ladder_skips_free_models_for_heavy_tasks(tmp_path):
    _, hub, _ = make_project(tmp_path)
    _implement_agents(hub, tmp_path)
    agents = hub.store.list_agents()
    catalog = {a.agent_id: list(a.models or []) for a in agents}
    policy = RolePolicy.default()
    light = policy.candidates("implement", agents=agents, catalog=catalog)
    heavy = policy.candidates("implement", agents=agents, catalog=catalog, heavy=True)
    assert any(c.model == "opencode/glm-5.3-flash-free" for c in light)
    assert all(c.model != "opencode/glm-5.3-flash-free" for c in heavy)
    assert any(c.model == "standard-model" for c in heavy)
    hub.close()


def test_recommender_skips_free_models_for_heavy_objective(tmp_path):
    _, hub, _ = make_project(tmp_path)
    _implement_agents(hub, tmp_path)
    light = hub.recommend_models(task_type="implementation")
    heavy = hub.recommend_models(task_type="implementation",
                                  objective="清明上河图三维场景重建与实机验收")
    light_names = {(c["agent_id"], c["model"]) for c in light["candidates"]}
    heavy_names = {(c["agent_id"], c["model"]) for c in heavy["candidates"]}
    assert ("heavy_worker", "opencode/glm-5.3-flash-free") in light_names
    assert ("heavy_worker", "opencode/glm-5.3-flash-free") not in heavy_names
    assert heavy["heavy"] is True


def test_chain_save_warns_when_heavy_stage_pins_free_model(tmp_path):
    _, hub, _ = make_project(tmp_path)
    _implement_agents(hub, tmp_path)
    result = hub.configure_chain(
        goal="清明上河图三维场景重建", stages=[{
            "agent_id": "heavy_worker",
            "model": "opencode/glm-5.3-flash-free",
            "type": "implementation",
            "title": "GLB 重建", "objective": "用 Blender 重建 buildings.glb",
        }], save_only=True)
    assert result["status"] == "pending_approval"
    assert result["warnings"], result
    assert "free" in result["warnings"][0]
    hub.close()