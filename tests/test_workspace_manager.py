from __future__ import annotations

from agentloop_hub.workspace import WorkspaceManager, initialize_project


def test_initialize_project_creates_git_repo(tmp_path):
    target = tmp_path / "new-project"
    info = initialize_project(target, name="Demo")
    assert info["created"] is True
    assert info["git_initialized"] is True
    assert info["initial_commit"] is True
    assert (target / ".git").exists()
    assert (target / "README.md").read_text(encoding="utf-8").startswith("# Demo")

    again = initialize_project(target)
    assert again["created"] is False
    assert again["git_initialized"] is False


def test_workspace_manager_opens_and_switches(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    manager = WorkspaceManager(None, auto_discover=False)
    assert manager.info()["open"] is False

    opened = manager.open(first, create=True, git_init=True, name="First")
    assert opened["switched"] is True
    assert manager.info()["open"] is True
    overview = manager.hub.overview()
    assert overview["project"]["root"] == str(first.resolve())

    manager.open(second, create=True, git_init=True, name="Second")
    assert manager.info()["root"] == str(second.resolve())
    assert manager.info()["project"]["name"] == "Second"
    manager.close()
    assert manager.info()["open"] is False
