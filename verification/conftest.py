"""Fixtures for the independent requirement verification suite.

Deliberately self-contained: it does not import helpers from ``tests/`` so a
pass here cannot be explained by the project's own test scaffolding.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MOCK = HERE / "mock_agent.py"
sys.path.insert(0, str(ROOT))


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


def build_repo(base: Path, name: str = "repo") -> Path:
    repo = base / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "verify@example.com")
    git(repo, "config", "user.name", "Verify")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    return repo


def agent_config(agent_id: str, *, plan_file: Path | None = None,
                 summary_file: Path | None = None, scenario_file: Path | None = None,
                 fail_times: int = 0) -> dict:
    env = {"MOCK_AGENT_ID": agent_id}
    if plan_file:
        env["MOCK_PLAN_FILE"] = str(plan_file)
    if summary_file:
        env["MOCK_SUMMARY_FILE"] = str(summary_file)
    if scenario_file:
        env["MOCK_SCENARIO_FILE"] = str(scenario_file)
    if fail_times:
        env["MOCK_FAIL_TIMES"] = str(fail_times)
    return {
        "command": [sys.executable, str(MOCK)],
        "prompt_mode": "stdin",
        "timeout_seconds": 60,
        "env": env,
    }


DEFAULT_SUMMARY = {
    "summary_version": 1,
    "completion": "verification run completed",
    "changes": ["calc.py"],
    "verification": "passed",
    "risks": [],
    "merge_recommendation": "approve",
    "next_actions": [],
}


def write_json(path: Path, data) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def plan(tasks: list[dict], *, goal: str = "verify requirement behaviour") -> dict:
    return {"plan_version": 1, "goal": goal, "constraints": [], "tasks": tasks}


def task(task_id: str, *, kind: str = "analysis", title: str = "", objective: str = "",
         agent: str = "", deps: list[str] | None = None, scope: list[str] | None = None,
         out_of_scope: list[str] | None = None, priority: int = 5,
         verify_cmds: list[str] | None = None,
         retry_limit: int = 0, risk: str = "low") -> dict:
    return {
        "task_id": task_id,
        "title": title or task_id,
        "type": kind,
        "objective": objective or f"{task_id} objective",
        "suggested_agent": agent,
        "dependencies": deps or [],
        "scope": scope or [],
        "out_of_scope": out_of_scope or [],
        "expected_outputs": ["summary"],
        "acceptance_criteria": ["evidence"],
        "verification_commands": verify_cmds or [],
        "token_budget": 20000,
        "time_limit_minutes": 5,
        "retry_limit": retry_limit,
        "risk_level": risk,
        "priority": priority,
    }


class Env:
    """Small harness around one Hub + registered mock CLI agents."""

    def __init__(self, hub, repo: Path, base: Path, *,
                 plan_file: Path | None = None, summary_file: Path | None = None,
                 scenario_file: Path | None = None):
        self.hub = hub
        self.repo = repo
        self.base = base
        self.plan_file = plan_file
        self.summary_file = summary_file
        self.scenario_file = scenario_file

    def register(self, agent_id: str, *, caps: dict | None = None, level: str = "L3",
                 **kwargs) -> None:
        capabilities = {
            "read_files": True, "write_files": True, "terminal": True,
            "web_search": False, "mcp": False, "session_read": False,
            "session_write": False, "structured_output": True,
        }
        capabilities.update(caps or {})
        self.hub.register_agent({
            "agent_id": agent_id, "name": agent_id.upper(), "product": agent_id,
            "adapter_type": "cli", "access_level": level,
            "adapter_config": agent_config(agent_id, **kwargs),
            "capabilities": capabilities, "models": ["mock/model-a"],
        })

    def run_plan(self, plan_data: dict, *, parallel: int = 2) -> dict:
        result = self.hub.import_plan(plan_data)
        approved = self.hub.decide_approval(result["approval_id"], approve=True)
        run_id = approved["outcome"]["run_id"]
        started = self.hub.start_run(run_id, max_parallel=parallel)
        return {"run_id": run_id, "plan_id": result["plan_id"], "started": started,
                "approval_id": result["approval_id"]}

    def set_scenario(self, data: dict) -> None:
        write_json(self.scenario_file, data)

    def set_plan(self, data: dict) -> None:
        write_json(self.plan_file, data)

    def task_rows(self, run_id: str) -> list[dict]:
        return self.hub.tasks(run_id=run_id)


@pytest.fixture()
def env(tmp_path: Path):
    from agentloop_hub.hub import Hub

    repo = build_repo(tmp_path)
    summary = write_json(tmp_path / "summary.json", DEFAULT_SUMMARY)
    plan_file = write_json(tmp_path / "plan.json", plan([]))
    scenario = write_json(tmp_path / "scenario.json", {})
    hub = Hub(repo, auto_discover=False)
    wrapped = Env(hub, repo, tmp_path, plan_file=plan_file, summary_file=summary,
                  scenario_file=scenario)
    wrapped.register("mock_main", plan_file=plan_file, summary_file=summary,
                     scenario_file=scenario)
    wrapped.register("mock_worker", plan_file=plan_file, summary_file=summary,
                     scenario_file=scenario)
    hub.set_main("mock_main", "mock/model-a")
    return wrapped
