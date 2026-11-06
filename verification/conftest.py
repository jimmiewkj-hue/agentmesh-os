"""Fixtures for the independent requirement verification suite.

Helpers live in ``verify_support`` so this module is never confused with
``tests/conftest.py`` when both suites are collected together.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_support import DEFAULT_SUMMARY, Env, build_repo, plan, write_json  # noqa: E402


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
