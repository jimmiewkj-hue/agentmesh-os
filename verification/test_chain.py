"""Independent tests for the multi-agent chain: agent + real model per stage.

Uses mock CLI agents (real subprocesses) so the chain mechanics are verified
without launching GUI apps.  The Trae/Qoder interface code is covered by
``test_trae_interface.py`` and the real-app E2E log.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import DEFAULT_SUMMARY, plan, task

from agentloop_hub.errors import ValidationError
from agentloop_hub.hub import Hub


def _scenario() -> dict:
    return {
        "CHAIN01": {"summary": "scan ok",
                    "evidence": [{"file": "calc.py", "lines": "1", "description": "scan"}]},
        "CHAIN02": {"summary": "review ok",
                    "evidence": [{"file": "calc.py", "lines": "1", "description": "review"}]},
        "CHAIN03": {"summary": "tests ok",
                    "evidence": [{"file": "tests/test_extra.py", "lines": "1",
                                  "description": "test"}],
                    "write_files": [{"path": "tests/test_extra.py",
                                     "content": "def test_ok():\n    assert True\n"}]},
    }


@pytest.fixture()
def chain_env(tmp_path: Path):
    from conftest import Env, build_repo, write_json

    repo = build_repo(tmp_path)
    summary = write_json(tmp_path / "summary.json", DEFAULT_SUMMARY)
    plan_file = write_json(tmp_path / "plan.json", plan([]))
    scenario = write_json(tmp_path / "scenario.json", _scenario())
    hub = Hub(repo, auto_discover=False)
    env = Env(hub, repo, tmp_path, plan_file=plan_file, summary_file=summary,
              scenario_file=scenario)
    for agent_id in ("mock_main", "scanner", "reviewer", "tester"):
        env.register(agent_id, plan_file=plan_file, summary_file=summary,
                     scenario_file=scenario)
    hub.set_main("mock_main", "mock/model-a")
    return env


STAGES = [
    {"agent_id": "scanner", "model": "mock/model-a", "type": "analysis"},
    {"agent_id": "reviewer", "model": "mock/model-a", "type": "review"},
    {"agent_id": "tester", "model": "mock/model-a", "type": "implementation"},
]


def test_chain_pins_each_stage_to_its_agent_and_model(chain_env):
    out = chain_env.hub.configure_chain(goal="scan -> review -> test", stages=STAGES,
                                       start=False)
    rows = {r["task_id"]: r for r in chain_env.hub.tasks(run_id=out["run_id"])}
    assert rows["CHAIN01"]["assigned_agent"] == "scanner"
    assert rows["CHAIN02"]["assigned_agent"] == "reviewer"
    assert rows["CHAIN03"]["assigned_agent"] == "tester"
    for agent_id in ("scanner", "reviewer", "tester"):
        assert chain_env.hub.get_agent(agent_id).extra["selected_model"] == "mock/model-a"
    assert [s["task_id"] for s in out["stages"]] == ["CHAIN01", "CHAIN02", "CHAIN03"]


def test_chain_rejects_a_model_outside_the_agent_catalog(chain_env):
    bad = [dict(STAGES[0], model="gpt-9-imaginary")] + STAGES[1:]
    with pytest.raises(ValidationError) as exc:
        chain_env.hub.configure_chain(goal="bad model", stages=bad, start=False)
    assert "真实模型清单" in str(exc.value)
    assert "available" in (exc.value.details or {})


def test_chain_runs_three_stages_in_dependency_order(chain_env):
    out = chain_env.hub.configure_chain(goal="scan -> review -> test", stages=STAGES,
                                       start=True, background=False, max_parallel=1)
    status = chain_env.hub.chain_status(out["run_id"])
    assert [s["status"] for s in status["stages"]] == ["completed"] * 3
    assert status["status"] == "pending_acceptance"
    # The generated test lives in the stage's isolated worktree until merge.
    rows = {r["task_id"]: r for r in chain_env.hub.tasks(run_id=out["run_id"])}
    generated = Path(rows["CHAIN03"]["workdir"]) / "tests" / "test_extra.py"
    assert generated.read_text(encoding="utf-8").startswith("def test_ok")
    assert Path(rows["CHAIN03"]["patch_path"]).read_text(encoding="utf-8")
    # dependency order: stage 2 starts only after stage 1 finished
    assert rows["CHAIN02"]["started_at"] >= rows["CHAIN01"]["finished_at"]
    assert rows["CHAIN03"]["started_at"] >= rows["CHAIN02"]["finished_at"]


def test_chain_status_reports_the_model_used_per_stage(chain_env):
    out = chain_env.hub.configure_chain(goal="scan -> review -> test", stages=STAGES,
                                       start=True, background=False)
    status = chain_env.hub.chain_status(out["run_id"])
    assert {s["model"] for s in status["stages"]} == {"mock/model-a"}
    assert {s["agent_id"] for s in status["stages"]} == {"scanner", "reviewer", "tester"}
    assert status["goal"] == "scan -> review -> test"


def test_http_chain_endpoint_configures_and_reports(chain_env):
    from agentloop_hub.api.server import JobManager, _make_handler

    handler = _make_handler(chain_env.hub, JobManager(), None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        body = json.dumps({"goal": "scan -> review -> test", "stages": STAGES,
                           "start": True, "background": False, "max_parallel": 1})
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/chain", data=body.encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=180) as response:
            created = json.loads(response.read().decode("utf-8"))
        assert created["run_id"]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/chain?run_id={created['run_id']}",
                timeout=30) as response:
            status = json.loads(response.read().decode("utf-8"))
        assert [s["status"] for s in status["stages"]] == ["completed"] * 3
    finally:
        server.shutdown()
        server.server_close()


def test_recommend_models_prefers_free_but_keeps_quality(chain_env):
    chain_env.hub.register_agent_models("scanner", ["mock/model-a", "opencode/probe-free"])
    rec = chain_env.hub.recommend_models(task_type="analysis", profile="cost_first")
    assert rec["recommended"] is not None
    assert rec["recommended"]["model"] == "opencode/probe-free"
    assert rec["recommended"]["cost"] == 0.0
    assert rec["recommended"]["cost_source"] == "free_model"


def test_recommend_models_exposes_quality_and_cost_sources(chain_env):
    rec = chain_env.hub.recommend_models(task_type="implementation", profile="balanced")
    assert rec["profile"] == "balanced"
    assert rec["recommended"]["cost_source"] in ("free_model", "trae_catalog_rate", "estimated")
    assert 0.0 <= rec["recommended"]["quality"] <= 1.0
    assert rec["note"]


def test_preflight_cli_agents_binds_workspace_without_opening_apps(chain_env):
    report = chain_env.hub.preflight_agents(
        [{"agent_id": "scanner", "model": "mock/model-a"},
         {"agent_id": "tester", "model": "mock/model-a"}],
        workspace=str(chain_env.repo), open_apps=False)
    assert report["total"] == 2
    assert report["ready"] == 2, report
    for item in report["agents"]:
        assert item["workspace_set"] is True
        assert item["model_set"] is True
        assert item["ok"] is True


def test_preflight_rejects_unknown_model(chain_env):
    report = chain_env.hub.preflight_agents(
        [{"agent_id": "scanner", "model": "not-a-real-model"}],
        workspace=str(chain_env.repo), open_apps=False)
    assert report["ready"] == 0
    assert "真实模型清单" in report["agents"][0]["error"]


def test_run_bindings_report_per_stage(chain_env):
    out = chain_env.hub.configure_chain(goal="scan -> review -> test", stages=STAGES,
                                       start=True, background=False)
    bindings = chain_env.hub.run_bindings(out["run_id"])
    assert len(bindings["stages"]) == 3
    assert {row["agent_id"] for row in bindings["stages"]} == {"scanner", "reviewer", "tester"}
    assert all(row["model"] == "mock/model-a" for row in bindings["stages"])
    assert all(row["workspace"] for row in bindings["stages"])


def test_recommend_models_prefers_free_but_keeps_quality(chain_env):
    chain_env.hub.register_agent_models("scanner", ["mock/model-a", "opencode/probe-free"])
    rec = chain_env.hub.recommend_models(task_type="analysis", profile="cost_first")
    assert rec["recommended"] is not None
    assert rec["recommended"]["model"] == "opencode/probe-free"
    assert rec["recommended"]["cost"] == 0.0
    assert rec["recommended"]["cost_source"] == "free_model"


def test_recommend_models_exposes_quality_and_cost_sources(chain_env):
    rec = chain_env.hub.recommend_models(task_type="implementation", profile="balanced")
    assert rec["profile"] == "balanced"
    assert rec["recommended"]["cost_source"] in ("free_model", "trae_catalog_rate", "estimated")
    assert 0.0 <= rec["recommended"]["quality"] <= 1.0
    assert rec["note"]


def test_preflight_cli_agents_binds_workspace_without_opening_apps(chain_env):
    report = chain_env.hub.preflight_agents(
        [{"agent_id": "scanner", "model": "mock/model-a"},
         {"agent_id": "tester", "model": "mock/model-a"}],
        workspace=str(chain_env.repo), open_apps=False)
    assert report["total"] == 2
    assert report["ready"] == 2, report
    for item in report["agents"]:
        assert item["workspace_set"] is True
        assert item["model_set"] is True
        assert item["ok"] is True


def test_preflight_rejects_unknown_model(chain_env):
    report = chain_env.hub.preflight_agents(
        [{"agent_id": "scanner", "model": "not-a-real-model"}],
        workspace=str(chain_env.repo), open_apps=False)
    assert report["ready"] == 0
    assert "真实模型清单" in report["agents"][0]["error"]


def test_run_bindings_report_per_stage(chain_env):
    out = chain_env.hub.configure_chain(goal="scan -> review -> test", stages=STAGES,
                                       start=True, background=False)
    bindings = chain_env.hub.run_bindings(out["run_id"])
    assert len(bindings["stages"]) == 3
    assert {row["agent_id"] for row in bindings["stages"]} == {"scanner", "reviewer", "tester"}
    assert all(row["model"] == "mock/model-a" for row in bindings["stages"])
    assert all(row["workspace"] for row in bindings["stages"])


def test_budget_is_advisory_and_never_blocks_by_default(chain_env):
    from types import SimpleNamespace

    chain_env.hub.set_project_budget(10)  # 明显不够的预算
    task = SimpleNamespace(uid="probe", spec=SimpleNamespace(token_budget=10 ** 9))
    allowed, reason = chain_env.hub.budget.can_start(task)
    assert allowed is True
    assert reason == ""


def test_model_sync_is_idempotent_and_reports_status(chain_env):
    first = chain_env.hub.sync_models()
    second = chain_env.hub.sync_models()
    assert first["status"] == "synced"
    assert second["synced_at"]
    status = chain_env.hub.models_sync_status()
    assert status["synced_at"]
    assert status["stale"] is False
    # 重复同步不会把模型行越滚越多
    first_total = sum(a["total"] for a in first["agents"])
    second_total = sum(a["total"] for a in second["agents"])
    assert first_total == second_total
