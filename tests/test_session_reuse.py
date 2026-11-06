"""Repeated chain nodes for one Agent share its session and worktree."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from hub_test_support import make_project

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
    # The session continues, but each node keeps its own input/output
    # boundary: T2 does not inherit T1's uncommitted/undeclared code changes.
    assert Path(second) != Path(first)
    assert (repo / ".agentloop" / "runs" / "run_reuse" / "worktrees" / "T2").exists()
    assert Path(first).exists()

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


def test_same_agent_linear_chain_reuses_clean_session_worktree(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    agent = hub.get_agent("mock_worker")
    task = _task(hub, tmp_path, "T2")
    task.spec.dependencies = ["T1"]
    session = {"resume": True, "session_id": "sess-1",
               "workdir": str(repo), "tasks": ["T1"]}
    workdir = hub.runner._prepare_workdir(
        task, {"base_commit": hub.project.commit}, agent, session)
    assert Path(workdir) == Path(repo)
    # Dirty tracked files are never treated as a valid snapshot.
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    task2 = _task(hub, tmp_path, "T3")
    task2.spec.dependencies = ["T1", "T2"]
    second = hub.runner._prepare_workdir(
        task2, {"base_commit": hub.project.commit}, agent,
        {"resume": True, "session_id": "sess-1", "workdir": str(repo),
         "tasks": ["T1", "T2"]})
    assert Path(second) != Path(repo)
    hub.close()


def test_missing_dependencies_are_composed_into_session_tree(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agentloop_hub.workspace import create_worktree, generate_patch, patch_files
    from hub_test_support import init_repo

    base = init_repo(tmp_path / "repo")
    patch_wt = tmp_path / "patch-wt"
    create_worktree(str(base), str(patch_wt), "HEAD")
    (patch_wt / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    patch = tmp_path / "dep.patch"
    generate_patch(str(patch_wt), str(patch))

    from hub_test_support import make_project
    (tmp_path / "proj").mkdir()
    repo, hub, _ = make_project(tmp_path / "proj")
    session_tree = tmp_path / "session-tree"
    create_worktree(str(repo), str(session_tree), "HEAD")
    task = Task(uid="run:T2", project_id=hub.project.project_id, plan_id="p",
                run_id="r",
                spec=TaskSpec(task_id="T2", title="t", objective="o",
                              type="implementation", scope=["calc.py"],
                              dependencies=["T1"]),
                status="queued")
    upstream = SimpleNamespace(task_id="T1", patch_path=str(patch))
    monkeypatch.setattr(hub.runner, "_upstream_task_order", lambda _task: [upstream])
    assert hub.runner._compose_missing_dependencies(task, str(session_tree), set())
    assert "return a + b" in (session_tree / "calc.py").read_text(encoding="utf-8")
    assert "calc.py" in patch_files(patch)
    hub.close()
