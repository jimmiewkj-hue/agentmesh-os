from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agentloop_hub.adapters.base import ExecutionRequest, ExecutionUsage
from agentloop_hub.adapters.cli import CliAgentAdapter
from agentloop_hub.adapters.opencode import OpenCodeAdapter
from agentloop_hub.adapters.relay import FileRelayAdapter, ManualAdapter
from agentloop_hub.models import AgentRecord, Task, TaskSpec
from agentloop_hub.util import sha256_file
from hub_test_support import MOCK_AGENT


def make_task(tmp_path, task_id="T1", *, type="implementation") -> Task:
    spec = TaskSpec(task_id=task_id, title="t", objective="o", type=type,
                    scope=["calc.py"], token_budget=1000, time_limit_minutes=1,
                    retry_limit=1)
    return Task(uid="run:T1", project_id="prj", plan_id="plan", run_id="run",
                spec=spec, status="queued", workdir=str(tmp_path),
                task_dir=str(tmp_path / ".task"), base_commit="abc")


def make_agent(config: dict, agent_id="a1", adapter_type="cli", level="L2") -> AgentRecord:
    return AgentRecord(agent_id=agent_id, name=agent_id, product="mock", project_id="prj",
                       adapter_type=adapter_type, access_level=level,
                       adapter_config=config,
                       capabilities={"read_files": True, "write_files": True,
                                     "terminal": True, "structured_output": True})


def prompt_file(tmp_path) -> Path:
    p = tmp_path / "prompt.md"
    p.write_text("Read the task package and fix calc.py.", encoding="utf-8")
    return p


def test_cli_adapter_runs_mock_and_reads_result(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    task_dir = tmp_path / ".task"
    task_dir.mkdir()
    prompt = prompt_file(tmp_path)
    result_path = task_dir / "result.json"
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({
        "tasks": {"T1": {
            "write_files": [{"path": "out.txt", "content": "hello"}],
            "summary": "wrote out.txt",
            "evidence": [{"file": "out.txt", "lines": "1", "description": "content"}],
        }}
    }), encoding="utf-8")
    agent = make_agent({
        "command": [sys.executable, str(MOCK_AGENT)],
        "args": ["--workdir", "{workdir}", "--prompt-file", "{prompt_file}",
                 "--result-file", "{result_file}", "--task-id", "{task_id}"],
        "prompt_mode": "file", "timeout_seconds": 60,
        "env": {"MOCK_AGENT_ID": "a1", "MOCK_SCENARIO_FILE": str(scenario)},
    })
    task = make_task(work)
    request = ExecutionRequest(project_id="prj", project_root=str(tmp_path),
                               task=task, workdir=str(work), task_dir=str(task_dir),
                               prompt_path=str(prompt), result_path=str(result_path))
    outcome = CliAgentAdapter(agent).execute(request)
    assert outcome.status == "completed"
    assert outcome.result_payload["summary"] == "wrote out.txt"
    assert outcome.result_payload["agent_id"] == "a1"
    assert (work / "out.txt").read_text(encoding="utf-8") == "hello"
    assert outcome.usage.estimated is True
    assert (task_dir / "logs" / "agent.stdout.log").exists()


def test_cli_adapter_reports_failure_and_retry_marker(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    task_dir = tmp_path / ".task"
    task_dir.mkdir()
    prompt = prompt_file(tmp_path)
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"tasks": {"T1": {"fail_once_marker": ".failed"}}}),
                        encoding="utf-8")
    agent = make_agent({
        "command": [sys.executable, str(MOCK_AGENT)],
        "args": ["--workdir", "{workdir}", "--prompt-file", "{prompt_file}",
                 "--result-file", "{result_file}", "--task-id", "{task_id}"],
        "prompt_mode": "file", "timeout_seconds": 60,
        "env": {"MOCK_AGENT_ID": "a1", "MOCK_SCENARIO_FILE": str(scenario)},
    })
    task = make_task(work)
    req = ExecutionRequest("prj", str(tmp_path), task, str(work), str(task_dir),
                           str(prompt), str(task_dir / "result.json"))
    first = CliAgentAdapter(agent).execute(req)
    assert first.status == "failed"
    second = CliAgentAdapter(agent).execute(req)
    assert second.status == "completed"


def test_cli_adapter_timeout(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    task_dir = tmp_path / ".task"
    task_dir.mkdir()
    prompt = prompt_file(tmp_path)
    agent = make_agent({
        "command": [sys.executable, "-c", "import time; time.sleep(5)"],
        "args": [], "prompt_mode": "stdin", "timeout_seconds": 1,
    })
    task = make_task(work)
    req = ExecutionRequest("prj", str(tmp_path), task, str(work), str(task_dir),
                           str(prompt), str(task_dir / "result.json"),
                           timeout_seconds=1)
    outcome = CliAgentAdapter(agent).execute(req)
    assert outcome.status == "timeout"


def test_cli_adapter_missing_executable(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    task_dir = tmp_path / ".task"
    task_dir.mkdir()
    prompt = prompt_file(tmp_path)
    agent = make_agent({"command": "definitely-not-an-agent-xyz"})
    task = make_task(work)
    req = ExecutionRequest("prj", str(tmp_path), task, str(work), str(task_dir),
                           str(prompt), str(task_dir / "result.json"))
    outcome = CliAgentAdapter(agent).execute(req)
    assert outcome.status == "failed"
    assert "不存在" in outcome.error


def test_opencode_parser_real_event_shape():
    stdout = "\n".join([
        json.dumps({"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}}),
        json.dumps({"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": "OK"}}),
        json.dumps({"type": "step_finish", "sessionID": "ses_1",
                    "part": {"type": "step-finish", "tokens": {
                        "total": 8404, "input": 6599, "output": 2, "reasoning": 11,
                        "cache": {"write": 0, "read": 1792}}, "cost": 0}}),
    ])
    usage, tools, session = CliAgentAdapter.parse_jsonl(stdout, "prompt", 100)
    assert usage.input_tokens == 6599
    assert usage.output_tokens == 2
    assert usage.cached_tokens == 1792
    assert usage.reasoning_tokens == 11
    assert usage.total_tokens == 8404
    assert session == "ses_1"
    assert tools == []


def test_opencode_adapter_health_and_missing_result_repair(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    task_dir = tmp_path / ".task"
    task_dir.mkdir()
    prompt = prompt_file(tmp_path)
    result_path = task_dir / "result.json"
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({
        "tasks": {"T1": {"summary": "ok", "evidence": [{"file": "calc.py", "lines": "1"}]}}
    }), encoding="utf-8")
    fake = tmp_path / "opencode.exe"
    fake.write_text("", encoding="utf-8")
    agent = make_agent({
        "executable": str(fake), "timeout_seconds": 60, "repair_attempts": 0,
    }, adapter_type="opencode", level="L3")
    adapter = OpenCodeAdapter(agent)
    assert adapter.health()["available"] is True
    # No opencode process can run the fake text file; outcome must be failed,
    # not an exception.
    task = make_task(work)
    req = ExecutionRequest("prj", str(tmp_path), task, str(work), str(task_dir),
                           str(prompt), str(result_path))
    outcome = adapter.execute(req)
    assert outcome.status == "failed"


def _opencode_scripted(monkeypatch, tmp_path, result_path, session_id, config=None):
    from agentloop_hub.adapters import opencode as oc
    from agentloop_hub.adapters.process import ProcessResult

    fake = tmp_path / "opencode.exe"
    fake.write_text("", encoding="utf-8")
    agent = make_agent({
        "executable": str(fake), "timeout_seconds": 60, "repair_attempts": 0,
        **(config or {}),
    }, adapter_type="opencode", level="L3")
    adapter = OpenCodeAdapter(agent)
    captured = {}

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        captured["cmd"] = list(cmd)
        result_path.write_text(json.dumps({"status": "completed", "summary": "ok"}),
                               encoding="utf-8")
        stdout = "\n".join([
            json.dumps({"type": "step_start", "sessionID": session_id,
                        "part": {"type": "step-start"}}),
            json.dumps({"type": "text", "sessionID": session_id,
                        "part": {"type": "text", "text": "OK"}}),
            json.dumps({"type": "step_finish", "sessionID": session_id,
                        "part": {"type": "step-finish",
                                 "tokens": {"total": 10, "input": 5, "output": 5},
                                 "cost": 0}}),
        ])
        return ProcessResult(command=list(cmd), exit_code=0, stdout=stdout,
                             stderr="", duration_ms=10)

    monkeypatch.setattr(oc, "run_process", fake_run)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    task_dir = tmp_path / ".task"
    task_dir.mkdir(exist_ok=True)
    prompt = prompt_file(tmp_path)
    return adapter, captured, work, task_dir, prompt


def test_opencode_chain_resume_uses_session_flag(tmp_path, monkeypatch):
    result_path = tmp_path / ".task" / "result.json"
    (tmp_path / ".task").mkdir()
    adapter, captured, work, task_dir, prompt = _opencode_scripted(
        monkeypatch, tmp_path, result_path, "ses_prior")
    req = ExecutionRequest("prj", str(tmp_path), make_task(work), str(work),
                           str(task_dir), str(prompt), str(result_path),
                           resume_session={"resume": True, "session_id": "ses_prior",
                                           "workdir": str(work)})
    outcome = adapter.execute(req)
    assert outcome.status == "completed"
    assert captured["cmd"][captured["cmd"].index("--session") + 1] == "ses_prior"
    assert "--continue" not in captured["cmd"]
    assert outcome.session_id == "ses_prior"
    assert outcome.extras["interface"]["resumed_session_id"] == "ses_prior"


def test_opencode_chain_resume_can_be_disabled(tmp_path, monkeypatch):
    result_path = tmp_path / ".task" / "result.json"
    (tmp_path / ".task").mkdir()
    adapter, captured, work, task_dir, prompt = _opencode_scripted(
        monkeypatch, tmp_path, result_path, "ses_new",
        config={"chain_reuse_agent_sessions": False})
    req = ExecutionRequest("prj", str(tmp_path), make_task(work), str(work),
                           str(task_dir), str(prompt), str(result_path),
                           resume_session={"resume": True, "session_id": "ses_prior"})
    outcome = adapter.execute(req)
    assert outcome.status == "completed"
    assert "--session" not in captured["cmd"]
    assert outcome.extras["interface"]["resumed_session_id"] == ""


def test_opencode_first_node_starts_fresh_session(tmp_path, monkeypatch):
    result_path = tmp_path / ".task" / "result.json"
    (tmp_path / ".task").mkdir()
    adapter, captured, work, task_dir, prompt = _opencode_scripted(
        monkeypatch, tmp_path, result_path, "ses_first")
    req = ExecutionRequest("prj", str(tmp_path), make_task(work), str(work),
                           str(task_dir), str(prompt), str(result_path))
    outcome = adapter.execute(req)
    assert outcome.status == "completed"
    assert "--session" not in captured["cmd"]
    assert "--continue" not in captured["cmd"]
    assert outcome.extras["interface"]["resumed_session_id"] == ""


def test_opencode_resume_not_claimed_on_session_mismatch(tmp_path, monkeypatch):
    result_path = tmp_path / ".task" / "result.json"
    (tmp_path / ".task").mkdir()
    adapter, captured, work, task_dir, prompt = _opencode_scripted(
        monkeypatch, tmp_path, result_path, "ses_different")
    req = ExecutionRequest("prj", str(tmp_path), make_task(work), str(work),
                           str(task_dir), str(prompt), str(result_path),
                           resume_session={"resume": True, "session_id": "ses_prior"})
    outcome = adapter.execute(req)
    assert outcome.status == "completed"
    assert outcome.extras["interface"]["resumed_session_id"] == ""


def test_file_relay_waiting_then_collects(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    task_dir = tmp_path / ".task"
    task_dir.mkdir()
    (task_dir / "task.json").write_text("{}", encoding="utf-8")
    prompt = prompt_file(tmp_path)
    agent = make_agent({}, adapter_type="file_relay")
    task = make_task(work)
    req = ExecutionRequest("prj", str(tmp_path), task, str(work), str(task_dir),
                           str(prompt), str(task_dir / "result.json"))
    adapter = FileRelayAdapter(agent)
    first = adapter.execute(req)
    assert first.status == "waiting_user"
    outbox = Path(first.extras["relay_dir"]) / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    payload = {"task_id": "T1", "agent_id": "a1", "status": "completed", "summary": "done",
               "findings": [], "files_read": [], "files_changed": [], "commands_run": [],
               "tool_calls": [], "tests": [], "evidence": [], "risks": [],
               "unresolved_items": [], "artifacts": [], "token_usage": {},
               "cost_estimate": {}, "recommended_next_action": []}
    (outbox / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    second = adapter.execute(req)
    assert second.status == "completed"
    assert second.result_payload["summary"] == "done"


def test_manual_adapter_generates_handoff(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    task_dir = tmp_path / ".task"
    task_dir.mkdir()
    prompt = prompt_file(tmp_path)
    agent = make_agent({}, adapter_type="manual")
    task = make_task(work)
    req = ExecutionRequest("prj", str(tmp_path), task, str(work), str(task_dir),
                           str(prompt), str(task_dir / "result.json"))
    outcome = ManualAdapter(agent).execute(req)
    assert outcome.status == "waiting_user"
    assert Path(outcome.extras["handoff"]).exists()
