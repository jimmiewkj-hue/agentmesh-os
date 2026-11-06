"""S06: dependency composition is recorded as an input manifest."""

from __future__ import annotations

from pathlib import Path

from hub_test_support import default_scenario, make_project


def _scenario() -> dict:
    base = default_scenario()
    base["plan"] = {
        "plan_version": 1, "goal": "compose dependencies", "constraints": [],
        "tasks": [
            {"task_id": "A", "title": "first", "type": "implementation",
             "objective": "write one", "suggested_agent": "mock_worker",
             "dependencies": [], "scope": ["calc.py"], "out_of_scope": [],
             "verification_commands": [], "token_budget": 5000,
             "time_limit_minutes": 5, "retry_limit": 1, "risk_level": "low"},
            {"task_id": "B", "title": "second", "type": "implementation",
             "objective": "write two", "suggested_agent": "mock_worker",
             "dependencies": ["A"], "scope": ["calc.py"], "out_of_scope": [],
             "verification_commands": [], "token_budget": 5000,
             "time_limit_minutes": 5, "retry_limit": 1, "risk_level": "low"},
            {"task_id": "C", "title": "verify composition", "type": "verification",
             "objective": "prove B is on disk", "suggested_agent": "mock_verifier",
             "dependencies": ["B"], "scope": [], "out_of_scope": [],
             "verification_commands": [
                 'python -c "import sys; sys.exit(0 if \'two\' in open(\'calc.py\').read() else 7)"'
             ],
             "token_budget": 5000, "time_limit_minutes": 5, "retry_limit": 0,
             "risk_level": "low"},
        ],
    }
    base["tasks"] = {
        "A": {"write_files": [{"path": "calc.py",
                               "content": "def add(a, b):\n    return a + b  # one\n"}],
              "summary": "one", "evidence": [{"file": "calc.py", "lines": "1",
                                              "description": "one"}]},
        "B": {"write_files": [{"path": "calc.py",
                               "content": "def add(a, b):\n    return a + b  # two\n"}],
              "summary": "two", "evidence": [{"file": "calc.py", "lines": "2",
                                              "description": "two"}]},
        "C": {"summary": "verified", "files_read": ["calc.py"],
              "evidence": [{"file": "calc.py", "lines": "2", "description": "two"}]},
    }
    return base


def test_dependency_manifest_orders_artifacts_and_composes_the_tree(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path, scenario=_scenario())
    requested = hub.request_plan("compose dependencies", main_agent_id="mock_main",
                                 model="mock-model")
    approved = hub.decide_approval(requested["approval_id"], approve=True)
    run_id = approved["outcome"]["run_id"]
    hub.start_run(run_id, max_parallel=1, background=False)

    tasks = {t["task_id"]: t for t in hub.tasks(run_id=run_id)}
    assert tasks["C"]["status"] == "completed", tasks["C"]
    b_attempt = hub.store.list_attempts(tasks["B"]["uid"])[-1]
    assert [a["task_id"] for a in b_attempt["input_snapshot"]["manifest"]["artifacts"]] == ["A"]
    c_attempt = hub.store.list_attempts(tasks["C"]["uid"])[-1]
    artifacts = c_attempt["input_snapshot"]["manifest"]["artifacts"]
    assert [a["task_id"] for a in artifacts] == ["A", "B"]
    assert all(a["sha256"] for a in artifacts)
    # The verification node really sees B's change, i.e. the dependency chain
    # was composed before the command ran.
    composed = (Path(tasks["C"]["workdir"]) / "calc.py").read_text(encoding="utf-8")
    assert "# two" in composed
    assert c_attempt["input_snapshot"]["manifest_hash"] == \
        c_attempt["input_snapshot"]["manifest"]["manifest_hash"]
    hub.close()
