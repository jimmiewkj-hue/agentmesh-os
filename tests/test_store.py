from __future__ import annotations

import json
import sqlite3

import pytest

from agentloop_hub.errors import NotFoundError, ValidationError
from agentloop_hub.models import AgentRecord, Approval, MemoryRecord, Project
from agentloop_hub.storage.db import Database
from agentloop_hub.storage.repo import Store
from agentloop_hub.util import atomic_write_text, estimate_tokens, sha256_file, tree_hash


@pytest.fixture()
def store(tmp_path):
    db = Database(tmp_path / "state.db")
    return Store(db)


def make_project(tmp_path, name="p") -> Project:
    return Project(
        project_id="prj_1", name=name, root=str(tmp_path),
        workspace_id="ws_1", repository_id="repo_1", worktree_id="wt_1",
        branch="main", commit="abc",
    )


def test_atomic_write_and_hash(tmp_path):
    p = tmp_path / "a" / "b.txt"
    atomic_write_text(p, "hello")
    assert p.read_text(encoding="utf-8") == "hello"
    digest = sha256_file(p)
    assert len(digest) == 64
    assert estimate_tokens("hello world") >= 1


def test_tree_hash_changes_with_content(tmp_path):
    f1 = tmp_path / "a.txt"
    f1.write_text("a", encoding="utf-8")
    h1 = tree_hash(["a.txt"], base=tmp_path)
    f1.write_text("b", encoding="utf-8")
    h2 = tree_hash(["a.txt"], base=tmp_path)
    assert h1 != h2


def test_project_and_agent_crud(store, tmp_path):
    project = make_project(tmp_path)
    store.save_project(project)
    loaded = store.get_project("prj_1")
    assert loaded and loaded.name == "p"
    agent = AgentRecord(agent_id="a1", name="A", product="A", project_id="prj_1")
    store.upsert_agent(agent)
    assert store.get_agent("a1").name == "A"
    store.set_agent_status("a1", "offline", "x")
    assert store.get_agent("a1").status == "offline"
    store.delete_agent("a1")
    assert store.get_agent("a1") is None


def test_main_node_history(store):
    store.set_main_node("prj_1", "a1", "m1")
    store.set_main_node("prj_1", "a2", "m2")
    current = store.get_main_node("prj_1")
    assert current["agent_id"] == "a2"
    assert len(store.main_node_history("prj_1")) == 2


def test_audit_chain_and_append_only(store):
    for i in range(3):
        store.append_audit("prj_1", "user", f"act{i}", payload={"i": i})
    ok, errors = store.verify_audit_chain("prj_1")
    assert ok, errors
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("UPDATE audit SET action='tampered' WHERE seq=1")
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("DELETE FROM audit WHERE seq=1")
    # A direct hash rewrite is impossible through UPDATE; verify still catches
    # a chain built with a broken previous hash.
    store.db.execute("DROP TRIGGER audit_no_update")
    store.db.execute("UPDATE audit SET hash='bad' WHERE seq=2")
    ok2, errors2 = store.verify_audit_chain("prj_1")
    assert not ok2 and errors2


def test_approval_flow(store):
    approval = Approval(approval_id="apr_1", project_id="prj_1", kind="plan", subject="s")
    store.create_approval(approval)
    decided = store.decide_approval("apr_1", "approved", by="user", note="ok")
    assert decided.status == "approved"
    with pytest.raises(ValidationError):
        store.decide_approval("apr_1", "bad", by="user")
    with pytest.raises(NotFoundError):
        store.decide_approval("nope", "approved", by="user")


def test_usage_totals_and_breakdown(store):
    from agentloop_hub.models import UsageRecord
    store.add_usage(UsageRecord(record_id="u1", project_id="prj_1", agent_id="a1",
                                model="m", input_tokens=100, output_tokens=50, cost=0.1))
    store.add_usage(UsageRecord(record_id="u2", project_id="prj_1", agent_id="a1",
                                model="m", input_tokens=10, output_tokens=5, cost=0.01))
    totals = store.usage_totals("prj_1")
    assert totals["total_tokens"] == 165
    assert abs(totals["cost"] - 0.11) < 1e-9
    by_agent = store.usage_by_agent("prj_1")
    assert by_agent[0]["agent_id"] == "a1"


def test_memory_conflict_detection_and_resolution(store):
    base = dict(project_id="prj_1", type="project_fact", title="tech", source="scan")
    m1 = store.add_memory(MemoryRecord(memory_id="m1", body="python", **base))
    assert m1.status == "active"
    m2 = store.add_memory(MemoryRecord(memory_id="m2", body="node", **base))
    assert m2.status == "conflict"
    assert store.get_memory("m1").status == "conflict"
    store.resolve_memory_conflict("m2", keep=True)
    assert store.get_memory("m2").status == "active"
    assert store.get_memory("m1").status == "superseded"


def test_task_cache(store):
    store.cache_set("k", "prj_1", "analysis", "hash", {"x": 1})
    assert store.cache_get("k") == {"x": 1}
    store.cache_set("k2", "prj_1", "analysis", "hash", {"x": 1}, expires_at="2000-01-01T00:00:00Z")
    assert store.cache_get("k2") is None
