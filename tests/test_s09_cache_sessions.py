"""S09: approximate cache is reference-only; ownership and memory invalidation."""

from __future__ import annotations

from pathlib import Path

from hub_test_support import make_project

from agentloop_hub.control.cost import CostOptimizer, normalize_intent, similarity
from agentloop_hub.models import (
    ConversationRecord, MemoryStatus, MemoryType, Task, TaskSpec,
)
from agentloop_hub.sessions.common import ParsedSession, project_matches
from agentloop_hub.sessions.service import SessionImportService
from agentloop_hub.util import new_id, now_iso


def test_opposite_constraints_are_not_an_exact_match(tmp_path: Path):
    # The normalizer cannot distinguish these, so similarity is 1.0 -- exactly
    # why a near match must never become a successful cached result.
    assert similarity("balance > 0", "balance < 0") == 1.0
    repo, hub, _ = make_project(tmp_path)
    task = Task(uid="run:T1", project_id=hub.project.project_id, plan_id="p",
                run_id="r",
                spec=TaskSpec(task_id="T1", title="t", objective="balance > 0",
                              type="analysis", scope=["calc.py"]),
                status="queued")
    hub.cost.semantic_store(task, "fh", {"summary": "positive"})
    other = Task(uid="run:T2", project_id=hub.project.project_id, plan_id="p",
                 run_id="r",
                 spec=TaskSpec(task_id="T2", title="t", objective="balance < 0",
                               type="analysis", scope=["calc.py"]),
                 status="queued")
    reference = hub.cost.semantic_reference(other, "fh")
    assert reference is not None
    assert reference["reference_only"] is True
    # Lookup must not be recorded as a hit or a saving.
    stats = hub.store.semantic_stats(hub.project.project_id)
    assert int(stats.get("hits") or 0) == 0
    ledger = hub.store.savings_totals(hub.project.project_id)
    assert int(ledger.get("semantic_cache", {}).get("count", 0) or 0) == 0
    hub.close()


def test_parent_directory_is_not_project_ownership():
    assert project_matches("D:/qoder/agent-hub", "D:/qoder/agent-hub") is True
    assert project_matches("D:/qoder/agent-hub/sub", "D:/qoder/agent-hub") is True
    # A parent/sibling must not be attributed to the child project.
    assert project_matches("D:/qoder", "D:/qoder/agent-hub") is False
    assert project_matches("D:/qoder/other", "D:/qoder/agent-hub") is False


def test_include_unscoped_only_admits_sessions_without_a_cwd(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path)
    service = SessionImportService(hub.store, hub.project)
    no_cwd = ParsedSession(session_id="s1", source_path="x", messages=[])
    no_cwd.add_text("user", f"work in {repo}")
    assert service._scope_ok(no_cwd) is False  # text mention is a clue, not proof

    other = ParsedSession(session_id="s2", source_path="x", cwd="D:/qoder/other")
    assert service._scope_ok(other) is False

    inside = ParsedSession(session_id="s3", source_path="x", cwd=str(repo))
    assert service._scope_ok(inside) is True
    hub.close()


def _conversation(project_id: str, text: str) -> ConversationRecord:
    return ConversationRecord(
        conversation_id=new_id("conv"), project_id=project_id, agent_id="mock_worker",
        session_id="sess-1", source_path="src", title="demo", cwd="D:/x",
        started_at="", ended_at="", message_count=1, token_usage={},
        summary={"user_requests": [text], "last_assistant_reply": text,
                 "tool_names": ["Bash"]},
        raw_path="raw", imported_at=now_iso(), fingerprint="fp",
    )


def test_conversation_memory_is_redacted_and_not_injected(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path)
    record = _conversation(
        hub.project.project_id,
        "api_key=sk-abcdef1234567890 and Authorization: Bearer abcdef1234567890",
    )
    memory = hub.memory.sync_conversation(record)
    assert memory.injectable is False
    assert "sk-abcdef1234567890" not in memory.body
    assert "abcdef1234567890" not in memory.body
    injected = hub.memory.injectable()
    assert all(item["memory_id"] != memory.memory_id for item in injected)
    hub.close()


def test_retry_invalidates_previous_task_memories(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path)
    memory = hub.memory.add(
        type=MemoryType.KNOWLEDGE.value, title="old fix", body="old conclusion",
        task_uid="run:T1",
    )
    assert memory.status == MemoryStatus.ACTIVE.value
    count = hub.memory.invalidate_task_memories("run:T1", "任务重试：旧产物/结论已失效")
    assert count == 1
    assert hub.store.get_memory(memory.memory_id).status == MemoryStatus.EXPIRED.value
    hub.close()


def test_baseline_change_expires_dependent_knowledge(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path)
    memory = hub.memory.record_knowledge("calc fix", "add returns a-b on this baseline")
    assert "相关文件基线变化" in memory.invalid_conditions
    assert hub.memory.invalidate_on_baseline_change("aaaa1111", "bbbb2222") >= 1
    assert hub.store.get_memory(memory.memory_id).status == MemoryStatus.EXPIRED.value
    # Same baseline is not an invalidation event.
    assert hub.memory.invalidate_on_baseline_change("bbbb2222", "bbbb2222") == 0
    hub.close()
