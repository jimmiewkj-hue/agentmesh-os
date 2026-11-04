from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentloop_hub.config import HubConfig
from agentloop_hub.control.checkpoints import CheckpointService
from agentloop_hub.control.verification import Verifier
from agentloop_hub.models import Project, ResultPackage, Task, TaskSpec
from agentloop_hub.storage.db import Database
from agentloop_hub.storage.repo import Store
from agentloop_hub.util import new_id
from agentloop_hub.workspace import create_worktree, detect_repo, generate_patch
from agentloop_hub.workspace.identity import build_project
from agentloop_hub.control.paths import WorkspacePaths
from conftest import git


def make_task(repo: Path, *, scope=None, commands=None) -> Task:
    spec = TaskSpec(
        task_id="T1", title="fix", objective="fix calc", type="implementation",
        scope=scope or ["calc.py"], verification_commands=commands or [],
        token_budget=1000, time_limit_minutes=5,
    )
    return Task(uid="run:T1", project_id="prj", plan_id="plan", run_id="run",
                spec=spec, status="verifying", base_commit="HEAD",
                workdir=str(repo / ".agentloop" / "wt", ), task_dir=str(repo / ".agentloop" / "t"))


def build_verifier(repo: Path):
    info = detect_repo(repo)
    project = build_project(str(repo), project_id="prj", git_info=info)
    paths = WorkspacePaths(repo)
    paths.ensure()
    config = HubConfig(verify_build=False)
    return Verifier(project, paths, config)


def test_verifier_passes_with_patch_and_command(git_repo, tmp_path):
    verifier = build_verifier(git_repo)
    wt = tmp_path / "wt"
    create_worktree(str(git_repo), str(wt), "HEAD")
    (wt / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    patch = tmp_path / "p.patch"
    generate_patch(str(wt), str(patch))
    task = Task(
        uid="run:T1", project_id="prj", plan_id="plan", run_id="run",
        spec=TaskSpec(task_id="T1", title="fix", objective="fix", type="implementation",
                      scope=["calc.py"], verification_commands=["python -m unittest -q"]),
        status="verifying", base_commit="HEAD", workdir=str(wt),
        patch_path=str(patch), task_dir=str(tmp_path / "td"),
    )
    result = ResultPackage({
        "task_id": "T1", "agent_id": "a", "status": "completed", "summary": "fixed",
        "findings": [], "files_read": [], "files_changed": ["calc.py"], "commands_run": [],
        "tool_calls": [], "tests": [{"command": "python -m unittest -q", "result": "passed"}],
        "evidence": [{"file": "calc.py", "lines": "1-2", "description": "x"}],
        "risks": [], "unresolved_items": [], "artifacts": [],
        "token_usage": {}, "cost_estimate": {}, "recommended_next_action": [],
    })
    outcome = verifier.verify(task, result)
    assert outcome.report.status == "passed", outcome.report.to_dict()
    assert outcome.report.checks["scope_check"] == "passed"
    assert outcome.report.checks["patch_check"] == "passed"
    assert outcome.report.checks["unit_tests"] == "passed"


def test_analysis_task_failed_test_command_is_informational(git_repo):
    verifier = build_verifier(git_repo)
    task = Task(
        uid="run:T1", project_id="prj", plan_id="plan", run_id="run",
        spec=TaskSpec(task_id="T1", title="scan", objective="scan", type="analysis",
                      verification_commands=["python -m unittest -q"]),
        status="verifying", base_commit="HEAD", workdir=str(git_repo),
        task_dir=str(Path(git_repo) / ".agentloop" / "t"),
    )
    result = ResultPackage({
        "task_id": "T1", "agent_id": "a", "status": "completed", "summary": "scan",
        "findings": [], "files_read": ["calc.py"], "files_changed": [], "commands_run": [],
        "tool_calls": [], "tests": [], "evidence": [{"file": "calc.py", "lines": "1-2"}],
        "risks": [], "unresolved_items": [], "artifacts": [],
        "token_usage": {}, "cost_estimate": {}, "recommended_next_action": [],
    })
    outcome = verifier.verify(task, result)
    assert outcome.report.status == "warning"
    assert outcome.report.checks["unit_tests"] == "warning"
    assert any("信息性" in w for w in outcome.report.warnings)
    assert outcome.report.status != "failed"


def test_combine_ignores_skipped_checks_for_read_only_task(git_repo):
    from agentloop_hub.models import VerificationReport

    verifier = build_verifier(git_repo)
    task = Task(uid="run:T1", project_id="prj", plan_id="plan", run_id="run",
                spec=TaskSpec(task_id="T1", title="scan", objective="scan", type="analysis"),
                status="verifying", assigned_agent="executor")
    deterministic = VerificationReport(
        task_id="T1", verifier_agent="control-plane", status="passed",
        checks={"patch_check": "skipped", "unit_tests": "skipped"},
    )
    agent_report = VerificationReport(
        task_id="T1", verifier_agent="verifier", status="passed",
        checks={"scope_compliance": "passed", "build_tests": "warning",
                "patch_applicable": "skipped"},
    )
    combined = verifier.combine(task, deterministic, agent_report)
    assert combined.status == "passed"
    assert combined.checks["agent_build_tests"] == "passed"
    assert combined.checks["agent_patch_applicable"] == "passed"


def test_combine_filters_verification_noise(git_repo):
    from agentloop_hub.models import VerificationReport

    verifier = build_verifier(git_repo)
    task = Task(uid="run:T1", project_id="prj", plan_id="plan", run_id="run",
                spec=TaskSpec(task_id="T1", title="scan", objective="scan", type="analysis"),
                status="verifying", assigned_agent="executor")
    deterministic = VerificationReport(
        task_id="T1", verifier_agent="control-plane", status="passed",
        checks={"scope_check": "passed"}, warnings=["真实问题"],
    )
    agent_report = VerificationReport(
        task_id="T1", verifier_agent="verifier", status="warning",
        checks={"build_and_tests": "passed"},
        warnings=["__pycache__ 文件存在", "主工作区仍处于基线",
                  "Pre-fix test failure is informational only",
                  "Task made no code changes by design",
                  "The main workspace remains at the unpatched baseline"],
    )
    combined = verifier.combine(task, deterministic, agent_report)
    assert combined.warnings == ["真实问题"]
    assert combined.status == "warning"


def test_verifier_fails_scope_violation_and_missing_patch(git_repo, tmp_path):
    verifier = build_verifier(git_repo)
    wt = tmp_path / "wt2"
    create_worktree(str(git_repo), str(wt), "HEAD")
    (wt / "README.md").write_text("tampered\n", encoding="utf-8")
    patch = tmp_path / "bad.patch"
    generate_patch(str(wt), str(patch))
    task = Task(
        uid="run:T2", project_id="prj", plan_id="plan", run_id="run",
        spec=TaskSpec(task_id="T1", title="fix", objective="fix", type="implementation",
                      scope=["calc.py"]),
        status="verifying", base_commit="HEAD", workdir=str(wt),
        patch_path=str(patch), task_dir=str(tmp_path / "td2"),
    )
    result = ResultPackage({
        "task_id": "T1", "agent_id": "a", "status": "completed", "summary": "bad",
        "findings": [], "files_read": [], "files_changed": ["README.md"], "commands_run": [],
        "tool_calls": [], "tests": [], "evidence": [{"file": "README.md", "lines": "1"}],
        "risks": [], "unresolved_items": [], "artifacts": [],
        "token_usage": {}, "cost_estimate": {}, "recommended_next_action": [],
    })
    outcome = verifier.verify(task, result)
    assert outcome.report.status == "failed"
    assert outcome.report.checks["scope_check"] == "failed"
    # Missing patch for a writable task is a hard failure.
    task.patch_path = ""
    outcome2 = verifier.verify(task, result)
    assert outcome2.report.checks["patch_check"] == "failed"


def test_budget_and_pricing(tmp_path):
    from agentloop_hub.adapters.base import ExecutionUsage
    from agentloop_hub.control.budget import BudgetManager

    db = Database(tmp_path / "s.db")
    store = Store(db)
    project = Project(project_id="prj", name="p", root=str(tmp_path), workspace_id="w",
                      repository_id="r", worktree_id="t", extra={"token_budget": 200})
    store.save_project(project)
    config = HubConfig(pricing={"mock-model": {"input": 1.0, "output": 2.0}},
                       budget_hard_stop=True)
    budget = BudgetManager(store, project, config)
    task = Task(uid="run:T", project_id="prj", plan_id="plan", run_id="run",
                spec=TaskSpec(task_id="T", title="t", objective="o", token_budget=150,
                              scope=["a"]), status="queued")
    assert budget.can_start(task) == (True, "")
    usage = ExecutionUsage(input_tokens=100, output_tokens=50)
    record = budget.record(task, "a", "mock-model", usage, duration_ms=10)
    assert abs(record.cost - (100 / 1_000_000 * 1.0 + 50 / 1_000_000 * 2.0)) < 1e-9
    ok, reason = budget.can_start(task)
    assert not ok
    assert "预算" in reason
    assert budget.task_over_budget(task)[0] is False


def test_budget_counts_billable_tokens_and_warns_at_80_percent(tmp_path):
    from agentloop_hub.adapters.base import ExecutionUsage
    from agentloop_hub.control.budget import BudgetManager

    db = Database(tmp_path / "s.db")
    store = Store(db)
    project = Project(project_id="prj", name="p", root=str(tmp_path), workspace_id="w",
                      repository_id="r", worktree_id="t", extra={"token_budget": 200})
    store.save_project(project)
    budget = BudgetManager(store, project, HubConfig())
    task = Task(uid="run:T", project_id="prj", plan_id="plan", run_id="run",
                spec=TaskSpec(task_id="T", title="t", objective="o", token_budget=100),
                status="queued")
    budget.record(task, "a", "mock", ExecutionUsage(
        input_tokens=100, output_tokens=50, cached_tokens=1000), duration_ms=10)
    assert budget.used_project() == 150
    snapshot = budget.snapshot()
    assert snapshot["cached_tokens"] == 1000
    assert snapshot["billable_tokens"] == 150
    assert snapshot["warning"] == ""
    budget.record(task, "a", "mock", ExecutionUsage(input_tokens=20), duration_ms=10)
    assert budget.used_project() == 170
    warned = budget.snapshot()
    assert warned["warning"]
    assert warned["used_ratio"] >= 0.8


def test_checkpoint_service(tmp_path):
    db = Database(tmp_path / "s.db")
    store = Store(db)
    project = Project(project_id="prj", name="p", root=str(tmp_path), workspace_id="w",
                      repository_id="r", worktree_id="t")
    store.save_project(project)
    paths = WorkspacePaths(tmp_path)
    paths.ensure()
    from agentloop_hub.control.budget import BudgetManager
    budget = BudgetManager(store, project, HubConfig())
    service = CheckpointService(store, project, paths, budget)
    ckpt = service.create("test", extra={"x": 1})
    latest = service.latest()
    assert latest and latest["kind"] == "test"
    assert latest["snapshot"]["extra"]["x"] == 1
    assert Path(paths.checkpoints / f"{ckpt.checkpoint_id}.json").exists()


def test_context_levels_and_cap(tmp_path):
    from agentloop_hub.control.context import ContextBuilder
    db = Database(tmp_path / "s.db")
    store = Store(db)
    project = Project(project_id="prj", name="p", root=str(tmp_path), workspace_id="w",
                      repository_id="r", worktree_id="t")
    store.save_project(project)
    config = HubConfig(context_summary_tokens=80)
    ctx = ContextBuilder(store, project, config)
    detail = ctx.task_detail(
        Task(uid="run:T", project_id="prj", plan_id="plan", run_id="run",
             spec=TaskSpec(task_id="T", title="t", objective="o" * 500, scope=["a"]),
             status="queued"),
        level="summary",
    )
    assert detail["_truncated"] is True
    assert detail["task_id"] == "T"
    assert isinstance(detail, dict)
