"""Reverse-engineered context/effort options and trigger-time sync."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agentloop_hub.control import interfaces as iface
from agentloop_hub.control import trae_interface
from agentloop_hub.control.model_profile import resolve_profile, source_meta
from agentloop_hub.errors import ValidationError
from agentloop_hub.models import AgentRecord
from hub_test_support import make_project


def _agent(app: str) -> AgentRecord:
    return AgentRecord(
        agent_id=app, name=app, product=app,
        adapter_config={"gui_app": app},
    )


def _workbuddy_manifest(tmp_path: Path) -> dict:
    storage = tmp_path / "local_storage"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "product.info").write_text(json.dumps({
        "data": {"models": [{
            "id": "glm-5.3-flash", "name": "GLM-5.3-Flash",
            "vendor": "f", "credits": "x1",
            "contextWindow": {"defaultLength": 300000,
                              "supportedLengths": [300000, 600000, 1000000]},
            "maxAllowedSize": 1000000,
            "reasoning": {"supportedEfforts": ["low", "high", "max"],
                          "defaultEffort": "high",
                          "canDisableThinking": False},
        }]},
    }), encoding="utf-8")
    return {"session_db": str(tmp_path / "workbuddy.db")}


def _workbuddy_db(path: Path, workspace: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "create table sessions (id text primary key, cwd text, status text, "
        "mode text, model text, permission_mode text, source_mode text, "
        "created_at integer, context_window integer, thought_level text, "
        "updated_at integer)"
    )
    conn.execute(
        "insert into sessions values (?,?,?,?,?,?,?,?,?,?,?)",
        ("s1", workspace, "working", "craft", "glm-5.3-flash",
         "fullAccess", "craft", 1000, 300000, "high", 1),
    )
    conn.commit()
    conn.close()


def _qoder_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("create table chat_model_preferences (model_key text primary key, "
                 "visible integer, context_window integer, reasoning_effort text, "
                 "updated_at integer)")
    conn.execute("create table chat_sessions (session_id text primary key, "
                 "model text)")
    conn.execute("insert into chat_model_preferences values (?,?,?,?,?)",
                 ("dfmodel", 1, 1000000, "max", 1))
    conn.commit()
    conn.close()


def _trae_state(path: Path) -> None:
    catalog = {"solo_agent_1": [{
        "name": "deepseek-v4.1-flash",
        "display_name": "DeepSeek-V4.1-Flash",
        "config_source": 1,
        "context_window_size": {"max": [1000000], "default": 1000000},
        "reasoning_effort_config": {
            "support_thinking": True,
            "options": ["light", "high", "extra_high"],
            "default_level": "high",
        },
    }]}
    conn = sqlite3.connect(path)
    conn.execute("create table ItemTable (key text primary key, value blob)")
    uid = "1234567890"
    conn.execute("insert into ItemTable values (?,?)",
                 (f"{uid}:AI.agent.model.model_list_map", json.dumps(catalog)))
    conn.commit()
    conn.close()


def test_workbuddy_real_options_are_reverse_engineered(tmp_path):
    config = _workbuddy_manifest(tmp_path)
    rows = iface.list_workbuddy_models(config)
    row = next(item for item in rows if item["id"] == "glm-5.3-flash")
    assert row["context_options"] == [300000, 600000, 1000000]
    assert row["context_max"] == 1000000
    assert row["capability_options"] == ["low", "high", "max"]
    assert row["capability_default"] == "high"

    agent = _agent("workbuddy")
    meta = source_meta(agent, "GLM-5.3-Flash", config)
    assert meta["context_options"] == [300000, 600000, 1000000]
    assert meta["capability_labels"]["max"] == "极高"
    profile = resolve_profile(agent, "GLM-5.3-Flash", config,
                              context_length=600000, capability_level="极高")
    assert profile == {"context_length": 600000, "capability_level": "max"}
    with pytest.raises(ValidationError):
        resolve_profile(agent, "GLM-5.3-Flash", config,
                        context_length=500000)
    with pytest.raises(ValidationError):
        resolve_profile(agent, "GLM-5.3-Flash", config,
                        capability_level="xhigh")


def test_workbuddy_sync_writes_session_context_and_thought(tmp_path):
    workspace = str(tmp_path / "ws")
    config = _workbuddy_manifest(tmp_path)
    db = Path(config["session_db"])
    _workbuddy_db(db, workspace)

    outcome = iface.sync_workbuddy_session_profile(
        workspace=workspace, started_at=0, model="GLM-5.3-Flash",
        context_length=600000, capability_level="max", config=config,
    )
    assert outcome.ok, outcome.reason
    assert outcome.actual == {"context_length": 600000,
                              "capability_level": "max", "session_id": "s1"}

    bad = iface.sync_workbuddy_session_profile(
        workspace=workspace, started_at=0, model="GLM-5.3-Flash",
        context_length=500000, capability_level="max", config=config,
    )
    assert not bad.ok
    assert "300000, 600000, 1000000" in bad.reason


def test_workbuddy_user_id_matches_local_storage_suffix(tmp_path):
    db = tmp_path / "workbuddy.db"
    conn = sqlite3.connect(db)
    conn.execute("create table sessions (id text primary key, user_id text, "
                 "created_at integer)")
    conn.execute("insert into sessions values ('s1','uid-123',10)")
    conn.commit()
    conn.close()
    config = {"session_db": str(db)}
    assert iface.workbuddy_user_id(config) == "uid-123"


def test_workbuddy_sync_defers_until_session_is_persisted(tmp_path):
    config = _workbuddy_manifest(tmp_path)
    outcome = iface.sync_workbuddy_session_profile(
        workspace=str(tmp_path / "ws"), started_at=0,
        model="GLM-5.3-Flash", context_length=600000,
        capability_level="max", config=config, timeout_seconds=0.2,
    )
    assert outcome.ok
    assert outcome.actual["deferred"] is True
    assert "提交后补写" in outcome.evidence["reason"]


def test_qoder_sync_writes_context_and_effort(tmp_path):
    config = {"qoder_db": str(tmp_path / "main.sqlite")}
    db = Path(config["qoder_db"])
    _qoder_db(db)

    outcome = iface.sync_qoder_model_profile(
        model_key="dfmodel", context_length=500000,
        capability_level="极高", config=config,
    )
    assert outcome.ok, outcome.reason
    conn = sqlite3.connect(db)
    row = conn.execute(
        "select context_window, reasoning_effort from chat_model_preferences "
        "where model_key='dfmodel'").fetchone()
    conn.close()
    assert row == (500000, "xhigh")

    bad = iface.sync_qoder_model_profile(
        model_key="dfmodel", context_length=500000,
        capability_level="extra_high", config=config,
    )
    assert not bad.ok
    assert "xhigh" in bad.actual["options"]

    rows = iface.list_qoder_models(config)
    dfmodel = next(item for item in rows if item["id"] == "dfmodel")
    assert dfmodel["context_options"] == []
    assert dfmodel["context_max"] == 0
    assert dfmodel["context_observed_max"] == 500000
    assert dfmodel["capability_options"] == [
        "none", "minimal", "low", "medium", "high", "xhigh", "max"]


def test_catalog_selects_only_real_profile_values(tmp_path, monkeypatch):
    repo, hub, _ = make_project(tmp_path)
    rows = [{
        "id": "glm-5.3-flash", "name": "GLM-5.3-Flash",
        "context_options": [300000, 600000, 1000000], "context_max": 1000000,
        "capability_options": ["low", "high", "max"],
        "capability_default": "high", "current_context_length": 300000,
    }]
    monkeypatch.setattr(
        "agentloop_hub.control.model_catalog.read_source_models",
        lambda agent, config: rows,
    )
    record = hub.get_agent("mock_worker")
    hub.register_agent({
        "agent_id": "mock_worker", "name": record.name, "product": record.product,
        "adapter_type": record.adapter_type, "access_level": record.access_level,
        "adapter_config": {"gui_app": "workbuddy"},
        "capabilities": record.capabilities, "models": ["GLM-5.3-Flash"],
        "status": record.status,
    })
    result = hub.set_agent_model(
        "mock_worker", "GLM-5.3-Flash",
        context_length=600000, capability_level="极高",
    )
    assert result["capability_level"] == "max"
    entry = hub.agent_models()["by_agent"]["mock_worker"]
    assert entry["context_options"] == [300000, 600000, 1000000]
    assert entry["capability_labels"]["max"] == "极高"
    with pytest.raises(ValidationError):
        hub.set_agent_model("mock_worker", "GLM-5.3-Flash",
                            context_length=500000, capability_level="max")
    with pytest.raises(ValidationError):
        hub.set_agent_model("mock_worker", "GLM-5.3-Flash",
                            capability_level="xhigh")


def test_trae_effort_sync_writes_real_state_key(tmp_path):
    config = {"trae_state_db": str(tmp_path / "state.vscdb")}
    db = Path(config["trae_state_db"])
    _trae_state(db)

    rows = trae_interface.list_trae_models("trae_cn", config)
    row = next(item for item in rows if item["id"] == "deepseek-v4.1-flash")
    assert row["context_options"] == [1000000]
    assert row["capability_options"] == ["light", "high", "extra_high"]

    outcome = trae_interface.set_trae_reasoning_effort(
        model="deepseek-v4.1-flash", effort="extra_high",
        app="trae_cn", config=config,
    )
    assert outcome.ok, outcome.reason
    conn = sqlite3.connect(db)
    values = dict(conn.execute("select key, value from ItemTable"))
    conn.close()
    key = next(key for key in values if key.endswith(
        ":AI.agent.model.reasoning_effort_level_by_agent_model_v2"))
    mapping = json.loads(values[key])
    assert "extra_high" in mapping.values()

    bad = trae_interface.set_trae_reasoning_effort(
        model="deepseek-v4.1-flash", effort="max",
        app="trae_cn", config=config,
    )
    assert not bad.ok
    assert "extra_high" in bad.reason
