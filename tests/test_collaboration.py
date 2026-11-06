from __future__ import annotations

from types import SimpleNamespace

from agentloop_hub.control.collaboration import (
    build_contract, enforce_plan_contract, infer_role,
)
from agentloop_hub.models import AgentRecord, TaskPlan, TaskSpec


def _agents():
    return [
        AgentRecord(agent_id="trae_cn", name="Trae CN", product="Trae CN",
                    project_id="p", adapter_type="gui_automation",
                    access_level="L3", status="online",
                    extra={"selected_model": "deepseek-v4.1-flash"}),
        AgentRecord(agent_id="workbuddy", name="WorkBuddy", product="WorkBuddy",
                    project_id="p", adapter_type="gui_automation",
                    access_level="L3", status="online",
                    extra={"selected_model": "GLM-5.3-Flash"}),
        AgentRecord(agent_id="opencode", name="OpenCode", product="OpenCode",
                    project_id="p", adapter_type="opencode",
                    access_level="L3", status="online"),
    ]


def _plan(tasks):
    return TaskPlan(plan_id="plan_1", project_id="p", goal="清明上河图三维",
                    tasks=tasks)


def test_contract_only_uses_selected_subagent_and_main_review():
    store = SimpleNamespace(list_agents=_agents)
    contract = build_contract(store, None, "trae_cn", "deepseek-v4.1-flash")
    assert [s["agent_id"] for s in contract["sub_agents"]] == ["workbuddy"]
    assert contract["roles"]["implement"] == "workbuddy"
    assert contract["roles"]["review"] == "trae_cn"

    plan = _plan([
        TaskSpec(task_id="T1", title="扫描当前目录", objective="只读扫描",
                 type="analysis", suggested_agent="trae_cn"),
        TaskSpec(task_id="T2", title="实现三维场景", objective="编写页面",
                 type="implementation", scope=["src/**"],
                 suggested_agent="opencode"),
        TaskSpec(task_id="T3", title="核对史实与来源", objective="查公开资料",
                 type="analysis", suggested_agent="trae_cn"),
    ])
    notes = enforce_plan_contract(plan, contract)
    by_id = {t.task_id: t for t in plan.tasks}
    assert by_id["T1"].role == "explore"
    assert by_id["T1"].suggested_agent == "workbuddy"
    assert by_id["T1"].model == "GLM-5.3-Flash"
    assert by_id["T2"].role == "implement"
    assert by_id["T2"].suggested_agent == "workbuddy"
    assert by_id["T3"].role == "research"
    assert by_id["T3"].suggested_agent == "workbuddy"
    review = [t for t in plan.tasks if t.role == "review"]
    assert len(review) == 1
    assert review[0].suggested_agent == "trae_cn"
    assert review[0].model == "deepseek-v4.1-flash"
    assert notes
    plan.validate()


def test_infer_role_from_type_and_research_hint():
    assert infer_role(TaskSpec(task_id="a", title="x", objective="y",
                               type="verification")) == "verify"
    assert infer_role(TaskSpec(task_id="b", title="中文文档", objective="",
                               type="documentation")) == "write"
    assert infer_role(TaskSpec(task_id="c", title="核对史实与来源",
                               objective="查公开资料", type="analysis")) == "research"
    assert infer_role(TaskSpec(task_id="d", title="扫描目录",
                               objective="了解结构", type="analysis")) == "explore"


def test_contract_keeps_existing_review_and_does_not_duplicate():
    store = SimpleNamespace(list_agents=_agents)
    contract = build_contract(store, None, "trae_cn", "deepseek-v4.1-flash")
    plan = _plan([
        TaskSpec(task_id="T1", title="实现", objective="写页面",
                 type="implementation", scope=["src/**"]),
        TaskSpec(task_id="T2", title="主节点审阅", objective="只读审阅",
                 type="review", role="review"),
    ])
    enforce_plan_contract(plan, contract)
    assert len([t for t in plan.tasks if t.role == "review"]) == 1
    assert plan.tasks[1].suggested_agent == "trae_cn"


def test_role_words_in_type_are_normalized():
    from agentloop_hub.control.collaboration import normalize_plan_payload
    raw = {"plan_version": 1, "goal": "g", "tasks": [
        {"task_id": "T1", "title": "a", "objective": "a", "type": "research",
         "role": "research", "dependencies": [], "scope": []},
        {"task_id": "T2", "title": "b", "objective": "b", "type": "implement",
         "dependencies": [], "scope": ["src/**"]},
        {"task_id": "T3", "title": "c", "objective": "c", "type": "write",
         "dependencies": [], "scope": ["README.md"]},
        {"task_id": "T4", "title": "d", "objective": "d", "type": "verify",
         "dependencies": [], "scope": []},
    ]}
    normalize_plan_payload(raw)
    assert [t["type"] for t in raw["tasks"]] == [
        "analysis", "implementation", "documentation", "verification",
    ]
    assert raw["tasks"][1]["role"] == "implement"


def test_contract_aligns_type_with_role_and_normalizes_enums():
    from agentloop_hub.control.collaboration import normalize_plan_payload
    raw = {"plan_version": 1, "goal": "g", "tasks": [
        {"task_id": "T1", "title": "查资料", "objective": "来源", "type": "documentation",
         "role": "research", "approval_policy": "manual", "risk_level": "weird",
         "dependencies": [], "scope": ["docs/**"]},
    ]}
    normalize_plan_payload(raw)
    assert raw["tasks"][0]["approval_policy"] == "required"
    assert raw["tasks"][0]["risk_level"] == "medium"
    store = SimpleNamespace(list_agents=_agents)
    contract = build_contract(store, None, "trae_cn", "deepseek-v4.1-flash")
    plan = _plan([TaskSpec(task_id="T1", title="查资料", objective="来源",
                           type="documentation", role="research",
                           scope=["docs/**"])])
    enforce_plan_contract(plan, contract)
    assert plan.tasks[0].type == "documentation"
    assert plan.tasks[0].suggested_agent == "workbuddy"
