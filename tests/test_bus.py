from __future__ import annotations

import sqlite3

import pytest

from conftest import default_plan, make_project


def test_bus_publish_subscribe_poll_and_replay(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    first = hub.bus_publish("prompt", {"purpose": "external", "text": "hello"},
                           source="worker-a", correlation_id="c1")
    assert first["seq"] >= 1
    subscription = hub.bus_subscribe("worker-a", kinds=["prompt"])
    polled = hub.bus_poll(subscription["subscription_id"], limit=10)
    assert polled["count"] == 1
    assert polled["events"][0]["kind"] == "prompt"
    assert polled["cursor"] >= first["seq"]
    # A second poll consumes only new events.
    again = hub.bus_poll(subscription["subscription_id"], limit=10)
    assert again["count"] == 0
    hub.bus_publish("response", {"summary": "done"}, source="worker-a",
                    correlation_id="c1")
    second = hub.bus_poll(subscription["subscription_id"], limit=10)
    assert second["count"] == 0  # kind filter excludes response
    replay = hub.bus.replay(kind="prompt", limit=100)
    assert replay["count"] == 1
    hub.close()


def test_bus_state_changes_are_published_automatically(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    result = hub.import_plan(default_plan(str(tmp_path)))
    approved = hub.decide_approval(result["approval_id"], approve=True)
    run_id = approved["outcome"]["run_id"]
    task = next(t for t in hub.store.list_tasks(run_id) if t.task_id == "T001")
    hub.store.set_task_status(task.uid, "running")
    hub.store.set_task_status(task.uid, "failed", error="synthetic")
    kinds = {event["kind"] for event in hub.bus_events(limit=500)["events"]}
    assert {"workflow", "governance", "task_state"} & kinds
    task_events = [e for e in hub.bus_events(limit=500)["events"]
                   if e["kind"] == "task_state" and e["correlation_id"] == task.uid]
    assert task_events
    assert any(e["payload"]["status"] == "failed" for e in task_events)
    assert hub.bus_stats()["totals"]["events"] > 0
    hub.close()


def test_bus_is_append_only_and_hash_chain_verifies(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    hub.bus_publish("knowledge", {"title": "k"}, source="user")
    hub.bus_publish("workflow", {"status": "ok"}, source="user")
    assert hub.bus_verify()["ok"] is True
    event_id = hub.bus_events(limit=1)["events"][0]["event_id"]
    with pytest.raises(sqlite3.IntegrityError):
        hub.store.db.execute("DELETE FROM bus_events WHERE event_id=?", (event_id,))
    with pytest.raises(sqlite3.IntegrityError):
        hub.store.db.execute("UPDATE bus_events SET kind='x' WHERE event_id=?", (event_id,))
    hub.close()


def test_bus_rejects_unknown_kind_and_empty_subscriber(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    with pytest.raises(Exception):
        hub.bus_publish("nope", {"x": 1})
    with pytest.raises(Exception):
        hub.bus_subscribe("")
    hub.close()


def test_bus_payload_is_capped(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    hub.config.bus_payload_max_tokens = 256
    big = {"text": "x" * 5000, "findings": [{"summary": "y" * 500} for _ in range(100)]}
    event = hub.bus_publish("response", big, source="worker")
    assert event["payload"].get("_truncated") is True
    assert event["tokens"] > 0
    hub.close()
