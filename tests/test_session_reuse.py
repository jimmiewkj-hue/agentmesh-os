"""Repeated chain nodes for one Agent share its session and worktree."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from conftest import make_project

from agentloop_hub.models import Task, TaskSpec


def _task(hub, tmp_path: Path, task_id: str) -> Task:
    return Task(
        uid=f"run_reuse:{task_id}", project_id=hub.project.project_id,
        plan_id="plan", run_id="run_reuse",
        spec=TaskSpec(task_id=task_id, title=task_id, objective="x",
                      type="analysis", scope=[]),
        status="queued", workdir="", task_dir=str(tmp_path / task_id),
        base_commit=hub.project.commit,
    )


def test_repeated_agent_reuses_recorded_session_and_worktree(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    agent = hub.get_agent("mock_worker")
    run = {"base_commit": hub.project.commit}
    task1 = _task(hub, tmp_path, "T1")
    task2 = _task(hub, tmp_path, "T2")

    first = hub.runner._prepare_workdir(task1, run, agent, {})
    assert Path(first).name == "T1"
    hub.runner._record_agent_session(
        task1, agent, "worker-free", first,
        SimpleNamespace(session_id="sess-1", extras={}),
    )
    resume = hub.runner._resume_session(task2, agent, "worker-free")
    assert resume["session_id"] == "sess-1"
    assert resume["resume"] is True
    assert Path(resume["workdir"]) == Path(first)

    second = hub.runner._prepare_workdir(task2, run, agent, resume)
    assert Path(second) == Path(first)
    assert not (repo / ".agentloop" / "runs" / "run_reuse"
                / "worktrees" / "T2").exists()

    mismatch = hub.runner._resume_session(task2, agent, "another-model")
    assert mismatch["resume"] is False
    assert Path(mismatch["workdir"]) == Path(first)


def test_completed_node_is_committed_to_shared_worktree(tmp_path):
    from agentloop_hub.workspace import generate_patch, run_git

    repo, hub, _ = make_project(tmp_path)
    agent = hub.get_agent("mock_worker")
    task = Task(
        uid="run_reuse:T3", project_id=hub.project.project_id,
        plan_id="plan", run_id="run_reuse",
        spec=TaskSpec(task_id="T3", title="write a", objective="x",
                      type="implementation", scope=["a.txt"]),
        status="running", workdir="", task_dir=str(tmp_path / "T3"),
        base_commit=hub.project.commit,
    )
    Path(task.task_dir).mkdir(parents=True, exist_ok=True)
    workdir = hub.runner._prepare_workdir(
        task, {"base_commit": hub.project.commit}, agent, {})
    (Path(workdir) / "a.txt").write_text("hello\n", encoding="utf-8")
    patch = tmp_path / "T3.patch"
    generate_patch(workdir, patch)
    task.workdir = workdir
    task.patch_path = str(patch)
    hub.runner._commit_task_worktree(task)
    log = run_git(["log", "-1", "--pretty=%s"], cwd=workdir, check=False)
    assert "T3" in log.stdout_text
    dirty = run_git(["status", "--short"], cwd=workdir, check=False)
    assert dirty.stdout_text.strip() == ""
