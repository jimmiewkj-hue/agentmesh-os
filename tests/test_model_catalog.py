from __future__ import annotations

from hub_test_support import make_project


def test_catalog_lists_agent_models_and_selection(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    catalog = hub.agent_models()
    assert catalog["by_agent"]["mock_worker"]["models"]
    names = {m["name"] for m in catalog["by_agent"]["mock_worker"]["models"]}
    assert "mock-model" in names

    result = hub.set_agent_model("mock_worker", "mock-pro")
    assert result["selected_model"] == "mock-pro"
    agent = hub.get_agent("mock_worker")
    assert agent.extra["selected_model"] == "mock-pro"
    assert "mock-pro" in agent.models
    assert hub.runner._model_for_agent(agent) == "mock-pro"

    catalog = hub.agent_models()
    assert catalog["by_agent"]["mock_worker"]["selected_model"] == "mock-pro"
    assert any(m["source"] == "user" for m in catalog["by_agent"]["mock_worker"]["models"])

    removed = hub.remove_agent_model("mock_worker", "mock-pro")
    assert removed["selected_model"] == ""


def test_model_refresh_preserves_user_models(tmp_path, monkeypatch):
    repo, hub, _ = make_project(tmp_path)
    hub.set_agent_model("mock_worker", "user-model")
    monkeypatch.setattr(
        "agentloop_hub.control.model_catalog.detect_models_for_agent",
        lambda agent, ctx=None: ["detected-a", "detected-b"],
    )
    result = hub.refresh_agent_models("mock_worker")
    assert result["agents"][0]["detected"] == ["detected-a", "detected-b"]
    catalog = hub.agent_models()
    names = {m["name"] for m in catalog["by_agent"]["mock_worker"]["models"]}
    assert {"detected-a", "detected-b", "user-model"}.issubset(names)
    assert catalog["by_agent"]["mock_worker"]["selected_model"] == "user-model"


def test_models_with_the_same_display_label_are_not_dropped():
    from agentloop_hub.control.model_catalog import _dedupe_by_model_name

    rows = [
        {"name": "deepseek//deepseek-v4-pro", "display_name": "DeepSeek-V4-Pro"},
        {"name": "deepseek-v4-pro-0813", "display_name": "DeepSeek-V4-Pro"},
        {"name": "glm-5.3-flash", "display_name": "GLM-5.3-Flash"},
        {"name": "GLM-5.3-Flash", "display_name": "GLM-5.3-Flash"},
    ]
    kept = [item["name"] for item in _dedupe_by_model_name(rows)]
    assert kept == [
        "deepseek//deepseek-v4-pro", "deepseek-v4-pro-0813",
        "glm-5.3-flash", "GLM-5.3-Flash",
    ]
