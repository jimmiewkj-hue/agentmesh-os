from __future__ import annotations

from agentloop_hub.models import ConversationRecord
from conftest import default_plan, make_project


def test_project_index_and_code_structure_are_seeded(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    memories = hub.memory_list(limit=200)
    types = {m["type"] for m in memories}
    assert "project_index" in types
    assert "code_structure" in types
    titles = {m["title"] for m in memories}
    assert "项目索引" in titles
    assert "代码结构" in titles
    index = next(m for m in memories if m["title"] == "项目索引")
    assert "calc.py" in index["body"]
    hub.close()


def test_bootstrap_packet_contains_all_sync_levels(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    result = hub.import_plan(default_plan(str(tmp_path)))
    hub.decide_approval(result["approval_id"], approve=True)
    packet = hub.bootstrap(agent_id="mock_worker")
    assert packet["agent_id"] == "mock_worker"
    assert packet["project"]["root"] == str(repo)
    groups = packet["groups"]
    assert groups["project_index"]
    assert groups["code_structure"]
    assert groups["task_state"]
    assert groups["workflow"]
    assert packet["sync_levels"]["message"] >= 0
    assert packet["sync_levels"]["project_context"] > 0
    assert packet["sync_levels"]["task_state"] > 0
    hub.close()


def test_conversation_is_synced_into_unified_memory(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    record = ConversationRecord(
        conversation_id="conv_test", project_id=hub.project.project_id,
        agent_id="qoder", session_id="sess_1", source_path="x.jsonl",
        title="修复 calc", cwd=str(repo), message_count=3,
        token_usage={"input": 100, "output": 20},
        summary={"user_requests": ["fix calc"], "last_assistant_reply": "done",
                 "tool_names": ["read_file"]},
    )
    memory = hub.memory.sync_conversation(record)
    assert memory.type == "message"
    assert "qoder" in memory.title
    messages = hub.memory_list(type="message")
    assert messages and messages[0]["title"].startswith("会话 qoder")
    hub.close()


def test_sync_service_reports_five_levels(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    report = hub.run_sync()
    assert report["status"] == "synced"
    assert report["project_index"] and report["code_structure"]
    status = hub.sync_status()
    assert set(status["levels"]) == {1, 2, 3, 4, 5}
    assert status["levels"][3]["memory_records"] >= 2
    assert status["workflow"]["status"] == "no_run"
    hub.close()
