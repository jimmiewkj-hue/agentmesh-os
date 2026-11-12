"""Repeated chain nodes for one Agent share its session and worktree."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

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


def _conflict_chain(tmp_path):
    """A(worker) and B(verifier) edit the same line in parallel; C(worker)
    depends on both and must resume A's session tree, where B's patch
    cannot be composed cleanly."""
    from agentloop_hub.workspace import create_worktree, generate_patch
    from hub_test_support import git

    repo, hub, _ = make_project(tmp_path)
    base_commit = hub.project.commit

    wt_b = tmp_path / "wt-b"
    create_worktree(str(repo), str(wt_b), base_commit)
    (wt_b / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    patch_b = tmp_path / "B.patch"
    generate_patch(str(wt_b), str(patch_b))

    session_tree = tmp_path / "session-tree"
    create_worktree(str(repo), str(session_tree), base_commit)
    (session_tree / "calc.py").write_text("def add(a, b):\n    return a * b\n", encoding="utf-8")
    patch_a = tmp_path / "A.patch"
    generate_patch(str(session_tree), str(patch_a))
    git(session_tree, "add", "calc.py")
    git(session_tree, "commit", "-qm", "A session work")
    (session_tree / "draft.txt").write_text("keep me\n", encoding="utf-8")

    def spec(task_id, agent_id, deps):
        return {"task_id": task_id, "title": task_id, "type": "implementation",
                "objective": "edit calc", "suggested_agent": agent_id,
                "model": "mock-model", "dependencies": deps, "scope": ["calc.py"],
                "out_of_scope": [], "expected_outputs": ["code_patch"],
                "acceptance_criteria": [], "verification_commands": [],
                "token_budget": 5000, "time_limit_minutes": 5, "retry_limit": 1,
                "risk_level": "low"}

    plan = {"plan_version": 1, "goal": "conflict chain", "constraints": [],
            "tasks": [spec("A", "mock_worker", []),
                      spec("B", "mock_verifier", []),
                      spec("C", "mock_worker", ["A", "B"])]}
    imported = hub.import_plan(plan)
    hub.decide_approval(imported["approval_id"], approve=True)
    run_id = hub.latest_run()["run_id"]
    tasks = {t.task_id: t for t in hub.store.list_tasks(run_id)}
    hub.store.update_task(tasks["A"].uid, patch_path=str(patch_a))
    hub.store.update_task(tasks["B"].uid, patch_path=str(patch_b))
    hub.store.set_task_status(tasks["A"].uid, "completed", force=True)
    hub.store.set_task_status(tasks["B"].uid, "completed", force=True)
    agent = hub.get_agent("mock_worker")
    hub.runner._record_agent_session(
        hub.store.get_task(tasks["A"].uid), agent, "mock-model", str(session_tree),
        SimpleNamespace(session_id="sess-1", extras={}))
    return repo, hub, run_id, tasks, session_tree, patch_b


def test_compose_conflict_blocks_waiting_user_and_preserves_session(tmp_path):
    from agentloop_hub.util import read_json
    from agentloop_hub.workspace import run_git
    from hub_test_support import git

    repo, hub, run_id, tasks, session_tree, patch_b = _conflict_chain(tmp_path)
    head_before = git(session_tree, "rev-parse", "HEAD").strip()
    hub.runner._execute_task(tasks["C"].uid)

    blocked = hub.store.get_task(tasks["C"].uid)
    assert blocked.status == "waiting_user"
    assert blocked.error.startswith("SESSION_COMPOSE_CONFLICT:")
    assert blocked.attempts == 0  # no attempt was persisted; resume stays attempt-1
    run = hub.store.get_run(run_id)
    assert run["status"] == "waiting_user"
    assert "C" in run["blocked_reason"]

    # The session tree is restored to its exact pre-compose state.
    assert git(session_tree, "rev-parse", "HEAD").strip() == head_before
    content = (session_tree / "calc.py").read_text(encoding="utf-8")
    assert "return a * b" in content
    assert "<<<<<<<" not in content
    dirty = run_git(["status", "--porcelain", "-uno"], cwd=str(session_tree), check=False)
    assert dirty.stdout_text.strip() == ""
    assert (session_tree / "draft.txt").read_text(encoding="utf-8") == "keep me\n"

    # The recorded session survives, so continuation is still possible.
    sessions = read_json(hub.runner._agent_sessions_path(run_id), {})
    assert sessions["mock_worker"]["session_id"] == "sess-1"
    actions = [a["action"] for a in hub.runner.audit.list(limit=200)]
    assert "task.session_compose_blocked" in actions
    assert "task.session_dependency_failed" in actions
    hub.close()


def test_compose_conflict_resolved_manually_then_session_continues(tmp_path, monkeypatch):
    from hub_test_support import git

    repo, hub, run_id, tasks, session_tree, patch_b = _conflict_chain(tmp_path)
    hub.runner._execute_task(tasks["C"].uid)
    assert hub.store.get_task(tasks["C"].uid).status == "waiting_user"

    # The operator reproduces the conflict in the session tree, resolves it
    # and commits — exactly the recovery the block message asks for.
    from agentloop_hub.workspace import run_git
    applied = run_git(["apply", "--3way", str(patch_b)], cwd=str(session_tree), check=False)
    assert applied.returncode != 0
    (session_tree / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    git(session_tree, "add", "calc.py")
    git(session_tree, "commit", "-qm", "resolve B into session tree")

    task_c = hub.store.get_task(tasks["C"].uid)
    agent = hub.get_agent("mock_worker")
    resume = hub.runner._resume_session(task_c, agent, "mock-model")
    assert resume["session_id"] == "sess-1" and resume["resume"] is True
    workdir = hub.runner._prepare_workdir(
        task_c, hub.store.get_run(run_id), agent, resume, attempt_number=1,
        manifest=hub.runner._input_manifest(task_c))
    assert Path(workdir) == session_tree

    # "Continue run" requeues the blocked node without sending any request.
    monkeypatch.setattr(hub.runner, "start",
                        lambda *a, **k: {"status": "ready", "run_id": run_id})
    hub.runner.resume(run_id)
    requeued = hub.store.get_task(tasks["C"].uid)
    assert requeued.status == "queued"
    assert requeued.error == ""
    hub.close()
