"""Shared helpers for the project's own test suite.

Explicitly named so the independent ``verification`` suite never imports a
same-named ``conftest`` module.  Keeping helpers here (instead of importing
from ``conftest``) prevents import pollution when both suites are collected
together.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOCK_AGENT = Path(__file__).resolve().parent / "fixtures" / "mock_agent.py"


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


def mock_agent_config(scenario_path: Path, agent_id: str) -> dict:
    return {
        "command": [sys.executable, str(MOCK_AGENT)],
        "args": ["--workdir", "{workdir}", "--prompt-file", "{prompt_file}",
                 "--result-file", "{result_file}", "--task-id", "{task_id}"],
        "prompt_mode": "file",
        "timeout_seconds": 120,
        "env": {"MOCK_AGENT_ID": agent_id, "MOCK_SCENARIO_FILE": str(scenario_path)},
    }


def register_mock_agents(hub, scenario_path: Path, *, levels: dict[str, str] | None = None) -> None:
    levels = levels or {}
    for agent_id, name in (
        ("mock_main", "Mock Main"),
        ("mock_worker", "Mock Worker"),
        ("mock_verifier", "Mock Verifier"),
    ):
        hub.register_agent({
            "agent_id": agent_id, "name": name, "product": name,
            "adapter_type": "cli", "access_level": levels.get(agent_id, "L3"),
            "adapter_config": mock_agent_config(scenario_path, agent_id),
            "capabilities": {
                "read_files": True, "write_files": True, "terminal": True,
                "web_search": False, "mcp": False, "session_read": False,
                "session_write": False, "structured_output": True,
            },
            "models": ["mock-model"],
        })


def default_plan(root: str) -> dict:
    return {
        "plan_version": 1,
        "goal": "fix add and scan readme",
        "constraints": ["do not touch README"],
        "tasks": [
            {
                "task_id": "T001", "title": "scan calc", "type": "analysis",
                "objective": "read calc and report", "suggested_agent": "mock_worker",
                "dependencies": [], "scope": [], "out_of_scope": [],
                "expected_outputs": ["summary"], "acceptance_criteria": ["report"],
                "verification_commands": [], "token_budget": 5000,
                "time_limit_minutes": 5, "retry_limit": 1, "risk_level": "low",
            },
            {
                "task_id": "T002", "title": "scan readme", "type": "analysis",
                "objective": "read readme and report", "suggested_agent": "mock_verifier",
                "dependencies": [], "scope": [], "out_of_scope": [],
                "expected_outputs": ["summary"], "acceptance_criteria": ["report"],
                "verification_commands": [], "token_budget": 5000,
                "time_limit_minutes": 5, "retry_limit": 1, "risk_level": "low",
            },
            {
                "task_id": "T003", "title": "fix add", "type": "implementation",
                "objective": "fix add", "suggested_agent": "mock_worker",
                "dependencies": ["T001"], "scope": ["calc.py"],
                "out_of_scope": ["README.md"], "expected_outputs": ["code_patch"],
                "acceptance_criteria": ["tests pass"],
                "verification_commands": ["python -m unittest -q"],
                "token_budget": 10000, "time_limit_minutes": 10,
                "retry_limit": 1, "risk_level": "low",
            },
        ],
    }


def default_scenario() -> dict:
    return {
        "tasks": {
            "T001": {"sleep": 0.4, "summary": "calc has bug",
                     "findings": [{"severity": "high", "summary": "add subtracts"}]},
            "T002": {"sleep": 0.4, "summary": "readme ok"},
            "T003": {
                "write_files": [{"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"}],
                "summary": "fixed add",
                "tests": [{"command": "python -m unittest -q", "result": "passed"}],
                "evidence": [{"file": "calc.py", "lines": "1-2", "description": "return a + b"}],
            },
        },
        "verification": {"checks": {"scope_check": "passed", "build_check": "skipped",
                                    "unit_tests": "passed"}},
        "summary": {"summary_version": 1, "completion": "fixed add", "changes": ["calc.py"],
                    "verification": "passed", "risks": [],
                    "merge_recommendation": "approve", "next_actions": []},
    }


def write_scenario(path: Path, scenario: dict | None = None) -> Path:
    path.write_text(json.dumps(scenario or default_scenario(), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def init_repo(repo: Path) -> Path:
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    return repo


def make_project(tmp_path: Path, *, scenario: dict | None = None):
    """Create a git repo + Hub + three mock CLI agents + scenario."""
    from agentloop_hub.hub import Hub

    repo = init_repo(tmp_path / "repo")
    scenario = scenario or default_scenario()
    scenario = json.loads(json.dumps(scenario))
    scenario.setdefault("plan", default_plan(str(repo)))
    scenario_path = write_scenario(tmp_path / "scenario.json", scenario)
    hub = Hub(repo, auto_discover=False)
    register_mock_agents(hub, scenario_path)
    hub.set_main("mock_main", "mock-model")
    return repo, hub, scenario_path


__all__ = [
    "ROOT", "MOCK_AGENT", "git", "mock_agent_config", "register_mock_agents",
    "default_plan", "default_scenario", "write_scenario", "init_repo", "make_project",
]
