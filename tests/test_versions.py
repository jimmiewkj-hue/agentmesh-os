from __future__ import annotations

from conftest import make_project


def test_version_ledger_and_drift_detection(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    history = hub.agent_versions("mock_worker")["versions"]
    assert history and history[0]["version"] == "unknown"

    # Same fingerprint only refreshes last_seen.
    agent = hub.get_agent("mock_worker")
    hub.versions.record([agent])
    assert len(hub.agent_versions("mock_worker")["versions"]) == len(history)

    # Simulate an Agent upgrade that loses write capability.
    agent.version = "9.9.9"
    agent.capabilities = dict(agent.capabilities)
    agent.capabilities["write_files"] = False
    hub.store.upsert_agent(agent)
    hub.versions.record([agent])

    status = hub.agent_version_status()
    item = next(a for a in status["agents"] if a["agent_id"] == "mock_worker")
    assert item["compat_status"] == "drift"
    assert status["drift_count"] >= 1
    drifted = next(d for d in status["drifts"] if d["agent_id"] == "mock_worker")
    assert "write_files" in drifted["changes"]["capabilities_removed"]

    latest = hub.agent_versions("mock_worker")["versions"][0]
    assert latest["version"] == "9.9.9"
    assert latest["changes"]["version_from"] == "unknown"
    assert latest["changes"]["version_to"] == "9.9.9"


def test_version_history_survives_discovery_merge(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    hub.versions.record([hub.get_agent("mock_worker")])
    hub.versions.record([hub.get_agent("mock_worker")])
    rows = hub.agent_versions("mock_worker")["versions"]
    assert len(rows) == 1
    assert rows[0]["first_seen"] == rows[0]["last_seen"] or rows[0]["last_seen"] >= rows[0]["first_seen"]
