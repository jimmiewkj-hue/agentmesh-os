from __future__ import annotations

from agentloop_hub.connectors import (
    CAPABILITY_VOCABULARY, CONNECTOR_PROFILES, agent_descriptor, connector_manifest,
    derive_capability_list, is_auto_executable, missing_capabilities,
    required_capabilities_for,
)
from agentloop_hub.models import AgentRecord, AdapterType, TaskSpec


def make_agent(agent_id: str, product: str, **caps) -> AgentRecord:
    base = {
        "read_files": True, "write_files": True, "terminal": True,
        "web_search": False, "mcp": True, "session_read": False,
        "session_write": False, "structured_output": True,
    }
    base.update(caps)
    return AgentRecord(
        agent_id=agent_id, name=product, product=product, adapter_type=AdapterType.CLI.value,
        access_level="L2", capabilities=base, status="online",
        adapter_config={"command": "fake"},
    )


def test_connector_manifest_has_required_shape():
    agents = [
        make_agent("agent_a", "Agent A", web_search=True),
        make_agent("agent_b", "Claude Code", write_files=False),
    ]
    manifest = connector_manifest(agents)
    assert manifest["protocol"] == "agentmesh.connector.v1"
    assert set(manifest["capability_vocabulary"]) == set(CAPABILITY_VOCABULARY)
    assert len(CONNECTOR_PROFILES) >= 11
    for item in manifest["agents"]:
        assert item["agent_id"]
        assert isinstance(item["capability"], list)
        assert set(item["capability"]).issubset(CAPABILITY_VOCABULARY)
    assert "analysis" in manifest["agents"][0]["capability"]
    assert "web_search" in manifest["agents"][0]["capability"]


def test_profile_assumptions_only_for_installed_products():
    claude = AgentRecord(
        agent_id="claude_code", name="Claude Code", product="Claude Code",
        adapter_type="cli", access_level="L3", status="online",
        adapter_config={"executable": "claude"},
        capabilities={"read_files": True, "write_files": True, "terminal": True},
    )
    caps = derive_capability_list(claude)
    assert {"architecture", "code_review", "code_generation"}.issubset(set(caps))

    offline = AgentRecord(
        agent_id="claude_code", name="Claude Code", product="Claude Code",
        adapter_type="cli", access_level="L1", status="offline",
        adapter_config={}, capabilities={"read_files": True},
    )
    offline_caps = derive_capability_list(offline)
    assert "architecture" not in offline_caps


def test_required_capabilities_by_task_type():
    implementation = TaskSpec(task_id="T", title="t", objective="o", type="implementation",
                              scope=["calc.py"])
    required = required_capabilities_for(implementation)
    assert {"read_files", "write_files", "terminal", "code_generation"}.issubset(set(required))

    review = TaskSpec(task_id="T2", title="t", objective="o", type="review",
                      required_capabilities=["web_search"])
    assert "code_review" in required_capabilities_for(review)
    assert "web_search" in required_capabilities_for(review)


def test_auto_executable_and_analysis_are_not_inferred_for_gui_relay():
    relay = AgentRecord(
        agent_id="trae", name="Trae", product="Trae", adapter_type="file_relay",
        access_level="L2", status="online",
        capabilities={"read_files": True, "write_files": True, "terminal": True},
        adapter_config={"mode": "file_relay"},
    )
    caps = derive_capability_list(relay)
    assert "analysis" not in caps
    assert "auto_executable" not in caps
    assert is_auto_executable(relay) is False
    descriptor = agent_descriptor(relay)
    assert descriptor["auto_executable"] is False

    cli = make_agent("cli_agent", "CLI Agent")
    cli_caps = derive_capability_list(cli)
    assert "analysis" in cli_caps
    assert "auto_executable" in cli_caps
    assert is_auto_executable(cli) is True

    # Explicit opt-in is the only way a relay Agent becomes auto executable.
    relay.extra = {"allow_auto_relay": True}
    assert is_auto_executable(relay) is True


def test_missing_capabilities_and_descriptor():
    agent = make_agent("agent_a", "Agent A", web_search=False)
    assert missing_capabilities(agent, ["read_files"]) == []
    assert "web_search" in missing_capabilities(agent, ["web_search"])
    descriptor = agent_descriptor(agent)
    assert descriptor["agent_id"] == "agent_a"
    assert "capability" in descriptor and "capabilities" not in descriptor
