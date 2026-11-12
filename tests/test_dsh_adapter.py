"""DeepSeek Harness headless adapter — mechanisms verified against real runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agentloop_hub.adapters import dsh as dsh_mod
from agentloop_hub.adapters.base import ExecutionRequest
from agentloop_hub.adapters.dsh import DshAdapter, load_dsh_credentials, resolve_dsh_install
from agentloop_hub.adapters.process import ProcessResult
from agentloop_hub.adapters.registry import create_adapter
from agentloop_hub.connectors import is_auto_executable
from agentloop_hub.control.model_health import classify_model_error
from agentloop_hub.models import AgentRecord, Task, TaskSpec


# Captured from real `dsh --profile headless --json` probes on this machine.
REAL_CREDENTIAL_ERROR = "\n".join([
    json.dumps({"type": "session", "sessionId": "session-4a330bc4", "cwd": "C:\\Users\\<user>"}),
    json.dumps({"type": "status", "phase": "turn_start", "turn": 1}),
    json.dumps({"type": "status", "phase": "turn_end", "turn": 1,
                "reason": {"kind": "error", "error": {
                    "message": 'llm-deepseek: no API key for provider route "deepseek-official"',
                    "code": "MISSING_CREDENTIAL"}}}),
    json.dumps({"type": "final", "text": ""}),
])

REAL_SUCCESS = "\n".join([
    json.dumps({"type": "session", "sessionId": "session-c9ed4787", "cwd": "C:\\temp"}),
    json.dumps({"type": "status", "phase": "turn_start", "turn": 1}),
    json.dumps({"type": "status", "phase": "step_start", "turn": 1, "step": 1}),
    json.dumps({"type": "thinking", "text": ""}),
    json.dumps({"type": "text", "text": "ok"}),
    json.dumps({"type": "status", "phase": "step_end", "turn": 1, "step": 1,
                "usage": {"inputTokens": 6292, "outputTokens": 2, "cacheReadTokens": 0,
                          "cacheWriteTokens": 0, "totalTokens": 6294}}),
    json.dumps({"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}}),
    json.dumps({"type": "final", "text": "ok"}),
])


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch):
    # Unit tests must not read the user's real key store.
    monkeypatch.setattr(dsh_mod, "load_dsh_credentials", lambda *a, **k: {})


def _agent(tmp_path, config=None):
    exe = tmp_path / "DeepSeek Harness.exe"
    exe.write_text("", encoding="utf-8")
    (tmp_path / "resources").mkdir(exist_ok=True)
    (tmp_path / "resources" / "app.asar").write_text("", encoding="utf-8")
    cfg = {"dsh_exe": str(exe), "executable": str(exe), "timeout_seconds": 60,
           "repair_attempts": 0, **(config or {})}
    return AgentRecord(agent_id="dsh", name="DeepSeek Harness", product="DeepSeek Harness",
                       project_id="prj", adapter_type="dsh", access_level="L3",
                       adapter_config=cfg, status="online",
                       capabilities={"read_files": True, "write_files": True, "terminal": True})


def _request(tmp_path, resume=None, model=""):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    task_dir = tmp_path / ".task"
    task_dir.mkdir(exist_ok=True)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("task", encoding="utf-8")
    spec = TaskSpec(task_id="T1", title="t", objective="o", type="implementation", scope=["calc.py"])
    task = Task(uid="run:T1", project_id="prj", plan_id="plan", run_id="run", spec=spec,
                status="queued", workdir=str(work), task_dir=str(task_dir), base_commit="abc")
    return ExecutionRequest("prj", str(tmp_path), task, str(work), str(task_dir),
                            str(prompt), str(task_dir / "result.json"),
                            model=model, resume_session=resume or {})


def test_resolve_install_needs_exe_and_asar(tmp_path):
    fake_exe = tmp_path / "DeepSeek Harness.exe"
    fake_exe.write_text("", encoding="utf-8")
    exe2, cli2 = resolve_dsh_install({"dsh_exe": str(fake_exe)})
    assert exe2 == str(fake_exe) and cli2 == ""  # no asar yet
    (tmp_path / "resources").mkdir()
    (tmp_path / "resources" / "app.asar").write_text("", encoding="utf-8")
    exe3, cli3 = resolve_dsh_install({"dsh_exe": str(fake_exe)})
    assert cli3.endswith("cli.js") and "app.asar" in cli3


def test_load_credentials_reads_refs(tmp_path):
    cred = tmp_path / ".credentials.yaml"
    cred.write_text(yaml.safe_dump({"version": 1, "refs": {
        "DEEPSEEK_API_KEY": "sk-test-123", "EMPTY_KEY": ""}}), encoding="utf-8")
    refs = load_dsh_credentials(cred)
    assert refs == {"DEEPSEEK_API_KEY": "sk-test-123"}
    assert load_dsh_credentials(tmp_path / "absent.yaml") == {}


def test_parse_events_real_credential_error():
    parsed = DshAdapter.parse_events(REAL_CREDENTIAL_ERROR)
    assert parsed["session_id"] == "session-4a330bc4"
    assert parsed["error_code"] == "MISSING_CREDENTIAL"
    assert "no API key" in parsed["error"]


def test_parse_events_real_success_camelcase_usage():
    parsed = DshAdapter.parse_events(REAL_SUCCESS)
    assert parsed["session_id"] == "session-c9ed4787"
    assert parsed["text"] == "ok"
    assert parsed["error"] == ""
    assert parsed["usage"].input_tokens == 6292
    assert parsed["usage"].output_tokens == 2
    assert parsed["usage"].total_tokens == 6294
    assert parsed["usage"].cost_known is False


def test_parse_events_cli_error_event():
    parsed = DshAdapter.parse_events(json.dumps({"type": "error", "message": "unknown option '--model'"}))
    assert parsed["error_code"] == "CLI_ERROR"
    assert "unknown option" in parsed["error"]


def test_command_patch_precedes_json_and_resume(tmp_path):
    adapter = DshAdapter(_agent(tmp_path))
    req = _request(tmp_path)
    cmd = adapter._base_command(req, "do it", patch_path="/tmp/p.yml",
                                resume_session_id="session-abc")
    assert cmd.index("--patch") < cmd.index("--json")
    assert cmd[cmd.index("--patch") + 1] == "/tmp/p.yml"
    assert cmd[cmd.index("--session-id") + 1] == "session-abc"
    assert "--model" not in cmd  # not a valid dsh option


def test_model_patch_pins_agent_default_model(tmp_path):
    adapter = DshAdapter(_agent(tmp_path))
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    req = _request(tmp_path, model="deepseek-v4-pro")
    patch_path = adapter._model_patch(req, log_dir)
    patch = yaml.safe_load(Path(patch_path).read_text(encoding="utf-8"))
    assert patch == [{"id": "agent-default-model",
                      "config": {"provider": "deepseek-official", "model": "deepseek-v4-pro"}}]
    # provider-prefixed model splits provider/model
    req2 = _request(tmp_path, model="zm-deepseek/deepseek-flash")
    patch2 = yaml.safe_load(Path(adapter._model_patch(req2, log_dir)).read_text(encoding="utf-8"))
    assert patch2[0]["config"] == {"provider": "zm-deepseek", "model": "deepseek-flash"}
    # no model -> no patch
    assert adapter._model_patch(_request(tmp_path), log_dir) == ""


def test_execute_missing_credential_fails_fast(tmp_path, monkeypatch):
    adapter = DshAdapter(_agent(tmp_path))
    req = _request(tmp_path)

    def fake_run(cmd, **kw):
        return ProcessResult(command=cmd, exit_code=1, stdout=REAL_CREDENTIAL_ERROR,
                             stderr="dsh: MISSING_CREDENTIAL: no API key", duration_ms=10)

    monkeypatch.setattr(dsh_mod, "run_process", fake_run)
    outcome = adapter.execute(req)
    assert outcome.status == "failed"
    assert outcome.error_code == "MISSING_CREDENTIAL"
    assert outcome.session_id == "session-4a330bc4"
    assert classify_model_error(outcome.error) == "auth_invalid"


def test_execute_invalid_model_is_model_unavailable(tmp_path, monkeypatch):
    adapter = DshAdapter(_agent(tmp_path))
    req = _request(tmp_path, model="bogus-model")

    def fake_run(cmd, **kw):
        assert "--patch" in cmd
        return ProcessResult(command=cmd, exit_code=1,
                             stdout=json.dumps({"type": "session", "sessionId": "session-x"}),
                             stderr="dsh: INVALID_REQUEST: The supported API model names are "
                                    "deepseek-flash, deepseek-v4-pro, but you passed bogus-model.",
                             duration_ms=10)

    monkeypatch.setattr(dsh_mod, "run_process", fake_run)
    outcome = adapter.execute(req)
    assert outcome.status == "failed"
    assert outcome.error_code == "MODEL_UNAVAILABLE"


def test_execute_completed_marks_resume(tmp_path, monkeypatch):
    adapter = DshAdapter(_agent(tmp_path))
    req = _request(tmp_path, resume={"resume": True, "session_id": "session-prior"})
    stdout = "\n".join([
        json.dumps({"type": "session", "sessionId": "session-prior"}),
        json.dumps({"type": "final", "text": "ok"}),
    ])

    def fake_run(cmd, **kw):
        assert "--session-id" in cmd
        Path(req.result_path).write_text(json.dumps({"status": "completed", "summary": "ok"}),
                                         encoding="utf-8")
        return ProcessResult(command=cmd, exit_code=0, stdout=stdout, stderr="", duration_ms=10)

    monkeypatch.setattr(dsh_mod, "run_process", fake_run)
    outcome = adapter.execute(req)
    assert outcome.status == "completed"
    assert outcome.session_id == "session-prior"
    assert outcome.extras["interface"]["resumed_session_id"] == "session-prior"
    assert outcome.result_payload is not None


def test_execute_resume_mismatch_not_claimed(tmp_path, monkeypatch):
    adapter = DshAdapter(_agent(tmp_path))
    req = _request(tmp_path, resume={"resume": True, "session_id": "session-prior"})
    stdout = json.dumps({"type": "session", "sessionId": "session-other"})

    def fake_run(cmd, **kw):
        Path(req.result_path).write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        return ProcessResult(command=cmd, exit_code=0, stdout=stdout, stderr="", duration_ms=10)

    monkeypatch.setattr(dsh_mod, "run_process", fake_run)
    outcome = adapter.execute(req)
    assert outcome.extras["interface"]["resumed_session_id"] == ""


def test_registry_and_auto_executable(tmp_path):
    agent = _agent(tmp_path)
    assert isinstance(create_adapter(agent), DshAdapter)
    assert is_auto_executable(agent) is True
    agent.status = "offline"
    assert is_auto_executable(agent) is False


def test_health_missing_install(tmp_path, monkeypatch):
    monkeypatch.setattr(dsh_mod, "resolve_dsh_install", lambda *a, **k: ("", ""))
    adapter = DshAdapter(_agent(tmp_path))
    assert adapter.health()["available"] is False