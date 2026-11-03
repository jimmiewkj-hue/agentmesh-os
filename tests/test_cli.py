from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import ROOT, default_plan, make_project


def cli(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(**__import__("os").environ)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run([sys.executable, "-m", "agentloop_hub", "--project", str(repo), *args],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=120)


def test_cli_status_and_agents(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    hub.close()
    proc = cli(repo, "status", "--json")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["project"]["root"] == str(repo)
    assert data["agents"]["total"] == 3
    proc = cli(repo, "agents", "--json")
    assert proc.returncode == 0
    agents = json.loads(proc.stdout)
    assert {a["agent_id"] for a in agents} >= {"mock_main", "mock_worker", "mock_verifier"}
    proc = cli(repo, "doctor", "--json")
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["ok"]


def test_cli_plan_import_and_approve(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    hub.close()
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(default_plan(str(repo))), encoding="utf-8")
    proc = cli(repo, "plan", "import", str(plan_path), "--json")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["status"] == "pending_approval"
    proc = cli(repo, "approval", "list", "--json")
    approvals = json.loads(proc.stdout)
    plan_approval = next(a for a in approvals if a["kind"] == "plan" and a["status"] == "pending")
    proc = cli(repo, "approval", "approve", plan_approval["approval_id"], "--json")
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["outcome"]["status"] == "approved"
    run_id = result["outcome"]["run_id"]
    proc = cli(repo, "run", "status", run_id, "--json")
    assert proc.returncode == 0
    assert len(json.loads(proc.stdout)["tasks"]) == 3


def test_cli_audit_and_costs(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    hub.close()
    proc = cli(repo, "audit", "--verify", "--json")
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["ok"]
    proc = cli(repo, "costs", "--json")
    assert proc.returncode == 0
    assert "project_budget" in json.loads(proc.stdout)


def test_cli_agentmesh_commands(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    hub.close()
    proc = cli(repo, "connectors", "--json")
    assert proc.returncode == 0, proc.stderr
    manifest = json.loads(proc.stdout)
    assert manifest["protocol"] == "agentmesh.connector.v1"
    proc = cli(repo, "route", "rank", "--task-type", "implementation", "--json")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["selected"]["agent_id"] != "mock_main"
    proc = cli(repo, "bus", "publish", "knowledge", "--payload", '{"title":"k"}', "--json")
    assert proc.returncode == 0, proc.stderr
    published = json.loads(proc.stdout)
    proc = cli(repo, "bus", "list", "--kind", "knowledge", "--json")
    assert proc.returncode == 0
    assert any(e["seq"] == published["seq"] for e in json.loads(proc.stdout)["events"])
    proc = cli(repo, "sync", "--json")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["status"] == "synced"
    proc = cli(repo, "benchmark", "--json")
    assert proc.returncode == 0, proc.stderr
    assert "agents" in json.loads(proc.stdout)
    proc = cli(repo, "memory", "bootstrap", "--agent", "mock_worker", "--json")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["groups"]["project_index"]
    proc = cli(repo, "models", "--json")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["by_agent"]["mock_worker"]["models"]
    proc = cli(repo, "versions", "--status", "--json")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["agents"]
    proc = cli(repo, "main", "auto", "--json")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["agent_id"] != "mock_main"


def test_cli_error_exit_code(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    hub.close()
    proc = cli(repo, "task", "show", "missing", "--json")
    assert proc.returncode == 2
    assert "not_found" in proc.stderr
