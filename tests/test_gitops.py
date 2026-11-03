from __future__ import annotations

from pathlib import Path

import pytest

from agentloop_hub.errors import GitOperationError
from agentloop_hub.workspace import (
    WorkspaceMonitor, apply_patch, changed_paths, create_worktree, detect_repo,
    diff_snapshots, generate_patch, is_git_repo, patch_files, remove_worktree,
    reverse_patch, snapshot_tree, status_porcelain,
)
from agentloop_hub.workspace.identity import (
    build_project, detect_commands, detect_tech_stack, workspace_id_for,
)


def test_detect_repo_and_identity(git_repo):
    info = detect_repo(git_repo)
    assert info["is_git"] and info["branch"] == "master"
    project = build_project(str(git_repo), project_id="prj_x", git_info=info)
    assert project.workspace_id.startswith("ws_")
    assert project.workspace_id == workspace_id_for(str(git_repo))


def test_tech_stack_and_commands(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"scripts":{"build":"tsc","test":"vitest"}}',
                                           encoding="utf-8")
    stack = detect_tech_stack(str(tmp_path))
    assert "python" in stack and "node" in stack
    build, test = detect_commands(str(tmp_path))
    assert build == "npm run build"
    # npm test wins when package.json has a test script
    assert test == "npm test"


def test_worktree_patch_apply_and_reverse(git_repo, tmp_path):
    base = (git_repo / ".git").exists()
    assert base
    wt = tmp_path / "wt"
    create_worktree(str(git_repo), str(wt), "HEAD")
    (wt / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (wt / "new.txt").write_text("new\n", encoding="utf-8")
    changes = changed_paths(str(wt))
    assert {c["path"] for c in changes} == {"calc.py", "new.txt"}
    patch = tmp_path / "change.patch"
    generate_patch(str(wt), str(patch))
    files = patch_files(str(patch))
    assert "calc.py" in files and "new.txt" in files
    ok, msg = apply_patch(str(git_repo), str(patch), check_only=True)
    assert ok, msg
    ok, msg = apply_patch(str(git_repo), str(patch))
    assert ok, msg
    assert "return a + b" in (git_repo / "calc.py").read_text(encoding="utf-8")
    assert (git_repo / "new.txt").exists()
    ok, msg = reverse_patch(str(git_repo), str(patch))
    assert ok, msg
    assert "return a - b" in (git_repo / "calc.py").read_text(encoding="utf-8")
    assert not (git_repo / "new.txt").exists()
    remove_worktree(str(git_repo), str(wt))


def test_patch_excludes_agentloop_task(git_repo, tmp_path):
    wt = tmp_path / "wt2"
    create_worktree(str(git_repo), str(wt), "HEAD")
    pkg = wt / ".agentloop-task" / "T1"
    pkg.mkdir(parents=True)
    (pkg / "prompt.md").write_text("x", encoding="utf-8")
    (wt / "calc.py").write_text("def add(a, b):\n    return 1\n", encoding="utf-8")
    patch = tmp_path / "p.patch"
    generate_patch(str(wt), str(patch))
    assert ".agentloop-task" not in patch_files(str(patch))
    assert "calc.py" in patch_files(str(patch))
    remove_worktree(str(git_repo), str(wt))


def test_worktree_target_exists_raises(git_repo, tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    with pytest.raises(GitOperationError):
        create_worktree(str(git_repo), str(target), "HEAD")


def test_snapshot_diff_and_monitor(git_repo):
    snap1 = snapshot_tree(git_repo)
    (git_repo / "calc.py").write_text("changed\n", encoding="utf-8")
    (git_repo / "extra.txt").write_text("x\n", encoding="utf-8")
    (git_repo / "README.md").unlink()
    snap2 = snapshot_tree(git_repo)
    changes = diff_snapshots(snap1, snap2)
    kinds = {(c.kind, c.path) for c in changes}
    assert ("modified", "calc.py") in kinds
    assert ("created", "extra.txt") in kinds
    assert ("deleted", "README.md") in kinds

    seen = []
    monitor = WorkspaceMonitor(git_repo, interval=0.2, on_change=seen.append)
    (git_repo / "extra.txt").write_text("y\n", encoding="utf-8")
    changes2 = monitor.poll_once()
    assert len(changes2) >= 1


def test_status_porcelain_parsing(git_repo):
    (git_repo / "calc.py").write_text("x\n", encoding="utf-8")
    status = status_porcelain(git_repo)
    assert status[0]["path"] == "calc.py"
    assert status[0]["status"].strip().startswith("M")
