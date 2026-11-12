"""Model health from real provider errors (2026-11-07 defects #3/#4).

Expired subscriptions must degrade the model in the catalog, block new pins,
and be skipped by the role ladder/recommender — until a probe or a successful
execution proves recovery.  Nothing here is guessed: every state transition
comes from an observed error text or a probe result.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentloop_hub.adapters.base import ExecutionOutcome
from agentloop_hub.control.model_health import (
    classify_model_error, degraded_entry, health_map, is_degraded, model_health,
    ok_entry,
)
from agentloop_hub.errors import ValidationError
from agentloop_hub.models import Task, TaskSpec
from hub_test_support import make_project


def test_classify_provider_errors():
    assert classify_model_error(
        "ProviderBusinessError: InvalidSubscription (account 2104654906)") == "subscription_invalid"
    assert classify_model_error("订阅已失效，请续费") == "subscription_invalid"
    assert classify_model_error("Error: subscription expired") == "subscription_invalid"
    assert classify_model_error("InvalidApiKey: bad key") == "auth_invalid"
    assert classify_model_error("HTTP 401 Unauthorized") == "auth_invalid"
    assert classify_model_error("quota exceeded") == "quota_exhausted"
    assert classify_model_error("余额不足") == "quota_exhausted"
    assert classify_model_error("connection reset") == ""
    assert classify_model_error("") == ""


def _task(model: str = "mock-model", agent: str = "mock_worker") -> Task:
    spec = TaskSpec(task_id="T1", title="t", objective="o", type="implementation",
                    scope=["calc.py"], model=model, token_budget=1000,
                    time_limit_minutes=1)
    return Task(uid="run:T1", project_id="prj", plan_id="plan", run_id="run",
                spec=spec, status="queued", assigned_agent=agent, base_commit="abc")


def test_failed_execution_degrades_model_and_success_recovers(tmp_path):
    _, hub, _ = make_project(tmp_path)
    runner = hub.runner
    agent = hub.store.get_agent("mock_worker")
    runner._update_model_health(
        agent, "mock-model",
        ExecutionOutcome(status="failed", error_code="PROVIDER_BUSINESS_ERROR",
                         error="ProviderBusinessError: InvalidSubscription"))
    agent = hub.store.get_agent("mock_worker")
    entry = model_health(agent, "mock-model")
    assert entry["status"] == "degraded"
    assert entry["reason"] == "subscription_invalid"
    assert entry["source"] == "execution"
    assert is_degraded(agent, "mock-model")
    # A non-health failure does not clear or create entries.
    runner._update_model_health(
        agent, "mock-model", ExecutionOutcome(status="failed", error="syntax error"))
    assert model_health(hub.store.get_agent("mock_worker"), "mock-model")["status"] == "degraded"
    # A completed run proves recovery.
    runner._update_model_health(
        agent, "mock-model", ExecutionOutcome(status="completed"))
    agent = hub.store.get_agent("mock_worker")
    assert model_health(agent, "mock-model")["status"] == "ok"
    assert not is_degraded(agent, "mock-model")
    hub.close()


def test_pinned_degraded_model_blocks_instead_of_silent_swap(tmp_path):
    _, hub, _ = make_project(tmp_path)
    agent = hub.store.get_agent("mock_worker")
    extra = dict(agent.extra or {})
    extra["model_health"] = {"mock-model": degraded_entry(
        "subscription_invalid", "InvalidSubscription", source="probe")}
    hub.store.update_agent_extra("mock_worker", extra)
    with pytest.raises(ValidationError) as excinfo:
        hub.runner._pinned_agent(_task())
    assert "降级" in excinfo.value.message
    assert "订阅失效" in excinfo.value.message
    hub.close()


def test_model_for_agent_and_verifier_skip_degraded(tmp_path):
    _, hub, _ = make_project(tmp_path)
    agent = hub.store.get_agent("mock_worker")
    hub.store.set_agent_models("mock_worker", ["dead-model", "good-model"])
    extra = dict(agent.extra or {})
    extra["selected_model"] = "dead-model"
    extra["model_health"] = {"dead-model": degraded_entry(
        "subscription_invalid", "InvalidSubscription", source="execution")}
    hub.store.update_agent_extra("mock_worker", extra)
    agent = hub.store.get_agent("mock_worker")
    assert hub.runner._model_for_agent(agent) == "good-model"
    assert hub.runner._verifier_model(agent) == "good-model"
    hub.close()


def test_role_ladder_skips_degraded_models(tmp_path):
    from agentloop_hub.control.roles import RolePolicy
    _, hub, _ = make_project(tmp_path)
    agent = hub.store.get_agent("mock_worker")
    extra = dict(agent.extra or {})
    extra["model_health"] = {"mock-model": degraded_entry(
        "auth_invalid", "401", source="probe")}
    hub.store.update_agent_extra("mock_worker", extra)
    agents = hub.store.list_agents()
    planner = RolePolicy.default()
    catalog = {a.agent_id: list(a.models or []) for a in agents}
    candidates = planner.candidates("implement", agents=agents, catalog=catalog)
    assert all(c.model != "mock-model" or c.agent_id != "mock_worker"
               for c in candidates)
    hub.close()


def test_catalog_exposes_health_and_sync_probes_zcode(tmp_path, monkeypatch):
    from agentloop_hub.adapters import zcode as zcode_module
    _, hub, _ = make_project(tmp_path)
    hub.register_agent({
        "agent_id": "zcode", "name": "ZCode", "product": "ZCode",
        "adapter_type": "zcode", "access_level": "L3",
        "adapter_config": {"mode": "zcode_cli"},
        "capabilities": {"read_files": True, "write_files": True, "terminal": True},
        "models": ["glm-5.2"],
    })
    probes: list[str] = []

    def fake_probe(self, model, *, timeout=60.0):
        probes.append(model)
        return {"ok": False, "status": "degraded", "reason": "subscription_invalid",
                "error": "ProviderBusinessError: InvalidSubscription"}

    monkeypatch.setattr(zcode_module.ZcodeAdapter, "probe_model", fake_probe)
    monkeypatch.setattr(hub.model_catalog, "_source_meta", lambda *a, **k: {})
    import agentloop_hub.control.model_catalog as catalog_module
    monkeypatch.setattr(catalog_module, "detect_models_for_agent",
                        lambda agent, ctx=None: ["glm-5.2"])
    result = hub.model_catalog.refresh(agent_id="zcode", source="sync")
    assert probes == ["glm-5.2"]
    row = result["agents"][0]
    assert row["health_probed"]["glm-5.2"]["reason"] == "subscription_invalid"
    catalog = hub.model_catalog.catalog()
    models = catalog["by_agent"]["zcode"]["models"]
    target = next(m for m in models if m["name"] == "glm-5.2")
    assert target["health"]["status"] == "degraded"
    # Recommender must not offer the degraded model.
    rec = hub.recommend_models(task_type="implementation")
    assert all(not (c["agent_id"] == "zcode" and c["model"] == "glm-5.2")
               for c in rec["candidates"])
    hub.close()


def test_sync_probe_opt_out_and_unknown_keeps_previous(tmp_path, monkeypatch):
    from agentloop_hub.adapters import zcode as zcode_module
    _, hub, _ = make_project(tmp_path)
    hub.register_agent({
        "agent_id": "zcode", "name": "ZCode", "product": "ZCode",
        "adapter_type": "zcode", "access_level": "L3",
        "adapter_config": {"mode": "zcode_cli", "probe_on_sync": False},
        "capabilities": {"read_files": True, "write_files": True},
        "models": ["glm-5.2"],
    })

    def fail_probe(self, model, *, timeout=60.0):
        raise AssertionError("probe must be skipped when probe_on_sync=false")

    monkeypatch.setattr(zcode_module.ZcodeAdapter, "probe_model", fail_probe)
    import agentloop_hub.control.model_catalog as catalog_module
    monkeypatch.setattr(catalog_module, "detect_models_for_agent",
                        lambda agent, ctx=None: ["glm-5.2"])
    hub.model_catalog.refresh(agent_id="zcode", source="sync")
    agent = hub.store.get_agent("zcode")
    assert health_map(agent) == {}
    hub.close()


def test_zcode_probe_model_classifies_subscription_error(tmp_path, monkeypatch):
    from agentloop_hub.adapters import zcode as zcode_module
    from agentloop_hub.adapters.process import ProcessResult

    provider_config = tmp_path / "provider_config.json"
    provider_config.write_text(json.dumps({"config": {
        "providerOrder": ["p1"],
        "providerConfigRules": {"providerRules": [
            {"providerId": "p1", "config": {"personalModelIds": ["glm-5.2"]}}]},
    }}), encoding="utf-8")
    builtin = tmp_path / "zcode-builtin.json"
    builtin.write_text("{}", encoding="utf-8")
    exe = tmp_path / "ZCode.exe"
    exe.write_text("stub", encoding="utf-8")
    cjs = tmp_path / "zcode.cjs"
    cjs.write_text("stub", encoding="utf-8")
    agent_cfg = {"zcode_exe": str(exe), "zcode_cjs": str(cjs),
                 "provider_config": str(provider_config),
                 "builtin_provider_config": str(builtin)}
    from agentloop_hub.models import AgentRecord
    adapter = zcode_module.ZcodeAdapter(AgentRecord(
        agent_id="zcode", name="ZCode", product="ZCode", adapter_type="zcode",
        adapter_config=agent_cfg))

    outcomes = iter([
        ProcessResult(command=["zcode"], exit_code=1, stdout="", stderr=(
            "ProviderBusinessError: InvalidSubscription"), duration_ms=10, pid=1),
        ProcessResult(command=["zcode"], exit_code=0, stdout=json.dumps(
            {"sessionId": "sess_1", "response": "OK"}), stderr="",
            duration_ms=10, pid=1),
    ])
    monkeypatch.setattr(zcode_module, "run_process", lambda *a, **k: next(outcomes))
    bad = adapter.probe_model("glm-5.2", timeout=5)
    assert bad["ok"] is False and bad["status"] == "degraded"
    assert bad["reason"] == "subscription_invalid"
    assert bad["pinned_provider"] == "p1"
    good = adapter.probe_model("glm-5.2", timeout=5)
    assert good["ok"] is True and good["status"] == "ok"