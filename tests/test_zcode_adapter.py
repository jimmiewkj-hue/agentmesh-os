from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentloop_hub.adapters.base import ExecutionRequest
from agentloop_hub.adapters.discovery import DiscoveryContext, discover_agents
from agentloop_hub.adapters.zcode import (
    ZcodeAdapter,
    build_pinned_provider_config,
    resolve_model_target,
)
from agentloop_hub.models import Task, TaskSpec


def make_task(tmp_path, task_id="T1") -> Task:
    spec = TaskSpec(task_id=task_id, title="t", objective="o", type="implementation",
                    scope=["calc.py"], token_budget=1000, time_limit_minutes=1,
                    retry_limit=1)
    return Task(uid="run:T1", project_id="prj", plan_id="plan", run_id="run",
                spec=spec, status="queued", workdir=str(tmp_path),
                task_dir=str(tmp_path / ".task"), base_commit="abc")


def make_agent(config: dict) -> object:
    from agentloop_hub.models import AgentRecord
    return AgentRecord(agent_id="zcode", name="ZCode", product="ZCode", project_id="prj",
                       adapter_type="zcode", access_level="L3", adapter_config=config,
                       capabilities={"read_files": True, "write_files": True,
                                     "terminal": True, "structured_output": True})


PROVIDER_CONFIG = {
    "schemaVersion": 1,
    "config": {
        "providerOrder": ["prov-a", "prov-b"],
        "providerConfigRules": {"providerRules": [
            {"providerId": "prov-a", "providerName": "a", "config": {
                "group": "standard-personal",
                "access": {"type": "api-key", "apiKey": "k-a"},
                "personalModelIds": ["glm-5.3", "GLM-5.3-Flash"],
                "modelOrder": ["glm-5.3", "GLM-5.3-Flash"],
            }},
            {"providerId": "prov-b", "providerName": "b", "config": {
                "group": "standard-personal",
                "access": {"type": "api-key", "apiKey": "k-b"},
            }},
        ]},
        "modelConfigRules": {"providerModelRules": [
            {"modelId": "glm-5.3", "config": {"enabled": False}, "providerId": "prov-a"},
            {"modelId": "GLM-5.3-Flash", "config": {"properties": {"contextWindow": 1000000}},
             "providerId": "prov-a"},
            {"modelId": "deepseek-v4-pro", "config": {}, "providerId": "prov-b"},
        ]},
    },
}


# ---------------------------------------------------------------------------
# Model target resolution
# ---------------------------------------------------------------------------

def test_resolve_model_target_prefers_provider_order():
    target = resolve_model_target("GLM-5.3-Flash", PROVIDER_CONFIG)
    assert target == ("prov-a", "GLM-5.3-Flash")
    # disabled model rules are not offered
    assert resolve_model_target("glm-5.3", PROVIDER_CONFIG) == ("prov-a", "glm-5.3")
    # qualified reference passes through
    assert resolve_model_target("prov-b/deepseek-v4-pro", PROVIDER_CONFIG) == \
        ("prov-b", "deepseek-v4-pro")
    assert resolve_model_target("nope", PROVIDER_CONFIG) is None
    assert resolve_model_target("", PROVIDER_CONFIG) is None


def test_resolve_slash_id_with_unknown_prefix_searches_real_providers():
    # 2026-11-07 defect #4: "z-ai/glm-5.2" carried a display prefix that is
    # not a provider id; pinning it put a nonexistent provider first and the
    # CLI fell back to a dead subscription.  The model part must be resolved
    # against the real provider rules instead.
    assert resolve_model_target("z-ai/GLM-5.3-Flash", PROVIDER_CONFIG) == \
        ("prov-a", "GLM-5.3-Flash")
    assert resolve_model_target("z-ai/no-such-model", PROVIDER_CONFIG) is None
    # A model id that itself contains a slash stays intact when a real
    # provider offers it verbatim.
    config = {"config": {
        "providerOrder": ["deepseek"],
        "providerConfigRules": {"providerRules": [
            {"providerId": "deepseek", "config": {
                "personalModelIds": ["/deepseek-v4-pro"]}}]},
    }}
    assert resolve_model_target("deepseek//deepseek-v4-pro", config) == \
        ("deepseek", "/deepseek-v4-pro")
    # Unknown prefix with an empty config cannot invent a provider.
    assert resolve_model_target("z-ai/glm-5.2", {"config": {}}) is None


# ---------------------------------------------------------------------------
# Pinned provider config
# ---------------------------------------------------------------------------

def test_build_pinned_provider_config_does_not_mutate_source():
    import copy
    snapshot = copy.deepcopy(PROVIDER_CONFIG)
    pinned = build_pinned_provider_config(PROVIDER_CONFIG, "prov-b", "deepseek-v4-pro")
    assert PROVIDER_CONFIG == snapshot, "source provider config must never be mutated"
    assert pinned["config"]["providerOrder"][0] == "prov-b"
    rules = {r["providerId"]: r for r in pinned["config"]["providerConfigRules"]["providerRules"]}
    assert rules["prov-b"]["config"]["personalModelIds"] == ["deepseek-v4-pro"]
    model_rules = {(r["providerId"], r["modelId"]): r for r
                   in pinned["config"]["modelConfigRules"]["providerModelRules"]}
    assert model_rules[("prov-b", "deepseek-v4-pro")]["config"]["enabled"] is True

    pinned_a = build_pinned_provider_config(PROVIDER_CONFIG, "prov-a", "GLM-5.3-Flash")
    rules_a = {r["providerId"]: r for r in pinned_a["config"]["providerConfigRules"]["providerRules"]}
    assert rules_a["prov-a"]["config"]["personalModelIds"][0] == "GLM-5.3-Flash"
    model_rules_a = {(r["providerId"], r["modelId"]): r for r
                     in pinned_a["config"]["modelConfigRules"]["providerModelRules"]}
    # the pinned model is force-enabled, its siblings are disabled for determinism
    assert model_rules_a[("prov-a", "GLM-5.3-Flash")]["config"]["enabled"] is True
    assert model_rules_a[("prov-a", "glm-5.3")]["config"]["enabled"] is False


# ---------------------------------------------------------------------------
# Headless JSON parsing
# ---------------------------------------------------------------------------

def test_parse_headless_json_accepts_real_payload_shape():
    payload = {
        "sessionId": "sess_46ef3e26-c973-43bc-b9d0-9c61a89b46ea",
        "traceId": "d9d3072f-715a-4935-966b-82b336594f57",
        "response": "AGENTMESH_ZCODE_OK_20261107",
        "usage": {"source": "provider", "modelRequestCount": 1,
                  "inputTokens": 19947, "outputTokens": 46, "totalTokens": 19993,
                  "cacheReadTokens": 0},
    }
    parsed = ZcodeAdapter.parse_headless_json(json.dumps(payload), "")
    assert parsed and parsed["sessionId"].startswith("sess_")
    assert parsed["response"] == "AGENTMESH_ZCODE_OK_20261107"
    # preamble lines must not break parsing
    noisy = "ZCode Built-in refreshed revision=43\n" + json.dumps(payload)
    assert ZcodeAdapter.parse_headless_json(noisy, "") is not None
    assert ZcodeAdapter.parse_headless_json("", "ProviderBusinessError: boom") is None


# ---------------------------------------------------------------------------
# Execution with a stubbed process
# ---------------------------------------------------------------------------

class FakeProc:
    def __init__(self, stdout="", stderr="", exit_code=0, duration_ms=1500):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.duration_ms = duration_ms
        self.timed_out = False
        self.pid = 4321
        self.error = ""
        self.command: list[str] = []


def _install(tmp_path, monkeypatch, calls: list, script):
    """Patch zcode.run_process with a scenario callback."""
    from agentloop_hub.adapters import zcode as zcode_module

    def fake_run_process(command, *, cwd, timeout, env=None, stdin_text=None, shell=False):
        calls.append({"command": list(command), "cwd": str(cwd), "env": dict(env or {}),
                      "timeout": timeout})
        return script(len(calls), command, cwd, env)

    monkeypatch.setattr(zcode_module, "run_process", fake_run_process)


@pytest.fixture()
def zcode_env(tmp_path):
    """Fake ZCode install + provider config + builtin config."""
    install = tmp_path / "Program Files" / "ZCode"
    (install / "resources" / "glm").mkdir(parents=True)
    exe = install / "ZCode.exe"
    exe.write_bytes(b"MZ")
    cjs = install / "resources" / "glm" / "zcode.cjs"
    cjs.write_text("/* cli core */", encoding="utf-8")
    provider = tmp_path / "provider_config.json"
    provider.write_text(json.dumps(PROVIDER_CONFIG), encoding="utf-8")
    builtin = tmp_path / "zcode-builtin.json"
    builtin.write_text("{}", encoding="utf-8")
    config = {
        "zcode_exe": str(exe),
        "zcode_cjs": str(cjs),
        "provider_config": str(provider),
        "builtin_provider_config": str(builtin),
        "timeout_seconds": 60,
        "model_retry_max_retries": 1,
    }
    return config, exe, cjs


def test_zcode_adapter_executes_with_model_pinning(tmp_path, monkeypatch, zcode_env):
    config, exe, cjs = zcode_env
    work = tmp_path / "work"
    work.mkdir()
    task_dir = tmp_path / ".task"
    task_dir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("fix calc", encoding="utf-8")
    result_path = task_dir / "result.json"
    calls: list = []

    def script(n, command, cwd, env):
        if n == 1:
            result_path.write_text(json.dumps({"summary": "done"}), encoding="utf-8")
            return FakeProc(stdout=json.dumps({
                "sessionId": "sess_abc123",
                "response": "wrote result",
                "usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120},
            }))
        raise AssertionError("repair pass must not run when the result file exists")

    _install(tmp_path, monkeypatch, calls, script)
    agent = make_agent(config)
    adapter = ZcodeAdapter(agent)
    task = make_task(work)
    req = ExecutionRequest("prj", str(tmp_path), task, str(work), str(task_dir),
                           str(prompt), str(result_path), model="prov-b/deepseek-v4-pro")
    outcome = adapter.execute(req)
    assert outcome.status == "completed", outcome.error
    assert outcome.session_id == "sess_abc123"
    assert outcome.text == "wrote result"
    assert outcome.usage.input_tokens == 100
    assert outcome.result_payload == {"summary": "done"}

    assert len(calls) == 1
    cmd, env = calls[0]["command"], calls[0]["env"]
    assert cmd[0] == str(exe) and cmd[1] == str(cjs)
    assert "-p" in cmd and "--json" in cmd and "--resume" not in cmd
    assert cmd[cmd.index("--cwd") + 1] == str(work)
    # model pinning is delivered through the temp provider config env pair
    assert env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"]
    assert env["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"]
    assert env["ELECTRON_RUN_AS_NODE"] == "1"
    assert env["ZCODE_MODEL_RETRY_MAX_RETRIES"] == "1"
    pinned = json.loads(Path(env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"]).read_text(encoding="utf-8"))
    assert pinned["config"]["providerOrder"][0] == "prov-b"
    # the user's real provider config stays untouched
    assert json.loads(Path(config["provider_config"]).read_text(encoding="utf-8")) == PROVIDER_CONFIG


def test_zcode_adapter_unresolvable_model_fails_loudly(tmp_path, monkeypatch, zcode_env):
    config, _, _ = zcode_env
    work = tmp_path / "work"
    work.mkdir()
    task_dir = tmp_path / ".task"
    task_dir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x", encoding="utf-8")
    calls: list = []
    _install(tmp_path, monkeypatch, calls, lambda *a, **k: FakeProc())
    agent = make_agent(config)
    adapter = ZcodeAdapter(agent)
    task = make_task(work)
    req = ExecutionRequest("prj", str(tmp_path), task, str(work), str(task_dir),
                           str(prompt), str(task_dir / "result.json"), model="missing-model")
    outcome = adapter.execute(req)
    assert outcome.status == "failed"
    assert outcome.error_code == "MODEL_UNAVAILABLE"
    assert "missing-model" in outcome.error
    assert calls == []


def test_zcode_adapter_resume_repairs_missing_result(tmp_path, monkeypatch, zcode_env):
    config, _, _ = zcode_env
    work = tmp_path / "work"
    work.mkdir()
    task_dir = tmp_path / ".task"
    task_dir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x", encoding="utf-8")
    result_path = task_dir / "result.json"
    calls: list = []

    def script(n, command, cwd, env):
        if n == 1:
            return FakeProc(stdout=json.dumps({
                "sessionId": "sess_res1", "response": "forgot the file",
                "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}}))
        result_path.write_text(json.dumps({"summary": "repaired"}), encoding="utf-8")
        return FakeProc(stdout=json.dumps({
            "sessionId": "sess_res1", "response": "repaired",
            "usage": {"inputTokens": 30, "outputTokens": 10, "totalTokens": 40}}))

    _install(tmp_path, monkeypatch, calls, script)
    agent = make_agent(config)
    adapter = ZcodeAdapter(agent)
    task = make_task(work)
    req = ExecutionRequest("prj", str(tmp_path), task, str(work), str(task_dir),
                           str(prompt), str(result_path), model="GLM-5.3-Flash",
                           resume_session={"session_id": "sess_res1", "workdir": str(work),
                                           "model": "GLM-5.3-Flash", "resume": True})
    outcome = adapter.execute(req)
    assert outcome.status == "completed", outcome.error
    assert outcome.result_payload == {"summary": "repaired"}
    assert outcome.usage.input_tokens == 40
    assert len(calls) == 2
    first_cmd, second_cmd = calls[0]["command"], calls[1]["command"]
    # the initial call already continues the prior chain session
    assert first_cmd[first_cmd.index("--resume") + 1] == "sess_res1"
    assert second_cmd[second_cmd.index("--resume") + 1] == "sess_res1"
    # the repair prompt reuses the same session and asks for the result file
    assert "-p" in second_cmd
    repair_prompt = second_cmd[second_cmd.index("-p") + 1]
    assert "did not produce a valid JSON object" in repair_prompt


def test_zcode_adapter_maps_provider_business_error(tmp_path, monkeypatch, zcode_env):
    config, _, _ = zcode_env
    work = tmp_path / "work"
    work.mkdir()
    task_dir = tmp_path / ".task"
    task_dir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x", encoding="utf-8")
    calls: list = []
    stderr = ("ProviderBusinessError: Your account does not have a valid CodingPlan"
              " subscription.")
    _install(tmp_path, monkeypatch, calls,
             lambda *a, **k: FakeProc(stdout="", stderr=stderr, exit_code=1))
    agent = make_agent(config)
    adapter = ZcodeAdapter(agent)
    task = make_task(work)
    req = ExecutionRequest("prj", str(tmp_path), task, str(work), str(task_dir),
                           str(prompt), str(task_dir / "result.json"))
    outcome = adapter.execute(req)
    assert outcome.status == "failed"
    assert outcome.error_code == "PROVIDER_BUSINESS_ERROR"
    assert "CodingPlan" in outcome.error


def test_zcode_adapter_timeout_and_health(tmp_path, monkeypatch, zcode_env):
    config, exe, cjs = zcode_env
    work = tmp_path / "work"
    work.mkdir()
    task_dir = tmp_path / ".task"
    task_dir.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x", encoding="utf-8")
    calls: list = []

    def script(n, command, cwd, env):
        proc = FakeProc()
        proc.timed_out = True
        proc.exit_code = -9
        return proc

    _install(tmp_path, monkeypatch, calls, script)
    agent = make_agent(config)
    adapter = ZcodeAdapter(agent)
    health = adapter.health()
    assert health["available"] is True
    assert health["cli_core"] == str(cjs)
    task = make_task(work)
    req = ExecutionRequest("prj", str(tmp_path), task, str(work), str(task_dir),
                           str(prompt), str(task_dir / "result.json"))
    outcome = adapter.execute(req)
    assert outcome.status == "timeout"
    assert outcome.error_code == "EXEC_TIMEOUT"


# ---------------------------------------------------------------------------
# Discovery upgrade
# ---------------------------------------------------------------------------

def test_discovery_upgrades_zcode_to_l3_cli(tmp_path):
    home = tmp_path / "home"
    (home / ".zcode" / "cli" / "rollout").mkdir(parents=True)
    (home / ".zcode" / "v2").mkdir(parents=True)
    (home / ".zcode" / "v2" / "provider_config.json").write_text(
        json.dumps(PROVIDER_CONFIG), encoding="utf-8")
    install = tmp_path / "Program Files" / "ZCode"
    (install / "resources" / "glm").mkdir(parents=True)
    (install / "ZCode.exe").write_bytes(b"MZ")
    (install / "resources" / "glm" / "zcode.cjs").write_text("/* core */", encoding="utf-8")
    ctx = DiscoveryContext(home=home, appdata=tmp_path / "appdata",
                           localappdata=tmp_path / "local", programfiles=install.parent)
    records = discover_agents("prj", ctx)
    zcode = next(r for r in records if r.agent_id == "zcode")
    assert zcode.adapter_type == "zcode"
    assert zcode.access_level == "L3"
    assert zcode.capabilities.get("structured_output") is True
    assert zcode.capabilities.get("auto_executable") is True
    cfg = zcode.adapter_config
    assert cfg["zcode_cjs"].endswith("zcode.cjs")
    assert cfg["permission_mode"] == "yolo"
    assert cfg["model_retry_max_retries"] == 1
    assert cfg["provider_config"].endswith("provider_config.json")
    assert zcode.models, "model catalog from provider config must be present"


def test_discovery_keeps_zcode_relay_without_cli_core(tmp_path):
    home = tmp_path / "home"
    (home / ".zcode" / "cli" / "rollout").mkdir(parents=True)
    install = tmp_path / "Program Files" / "ZCode"
    install.mkdir(parents=True)
    (install / "ZCode.exe").write_bytes(b"MZ")
    ctx = DiscoveryContext(home=home, appdata=tmp_path / "appdata",
                           localappdata=tmp_path / "local", programfiles=install.parent)
    records = discover_agents("prj", ctx)
    zcode = next(r for r in records if r.agent_id == "zcode")
    assert zcode.adapter_type == "file_relay"
    assert zcode.access_level == "L2"
