"""Project initialization/open helpers used by the CLI and Web console."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import ConfigError, ValidationError
from ..util import ensure_dir
from .gitops import detect_repo, is_git_repo, run_git


def initialize_project(path: str | Path, *, name: str = "", git_init: bool = True) -> dict[str, Any]:
    """Create (if needed) a directory and make it a Git workspace.

    Idempotent. Existing directories are never committed implicitly; only a
    newly created/empty directory gets one initial commit so worktrees can be
    created immediately.
    """
    root = Path(path).expanduser().resolve()
    created = False
    if root.exists() and not root.is_dir():
        raise ValidationError(f"目标路径不是目录: {root}")
    if not root.exists():
        ensure_dir(root)
        created = True
    was_empty = not any(root.iterdir())
    readme_created = False
    if name.strip() and was_empty and not (root / "README.md").exists():
        (root / "README.md").write_text(f"# {name.strip()}\n", encoding="utf-8")
        readme_created = True
    git_created = False
    if not is_git_repo(root):
        if not git_init:
            raise ConfigError(
                f"项目目录不是 Git 仓库: {root}。请先执行 git init，或允许控制台自动初始化。"
            )
        _git_init(root)
        git_created = True
    initial_commit = False
    if not detect_repo(root).get("commit") and (was_empty or readme_created):
        # A brand-new project needs at least one commit before worktrees can be
        # created. Existing user content is never committed implicitly.
        initial_commit = _initial_commit(root)
    return {
        "root": str(root), "created": created, "git_initialized": git_created,
        "initial_commit": initial_commit,
        "is_git": is_git_repo(root), "name": name.strip() or root.name,
    }


def _git_init(root: Path) -> None:
    try:
        run_git(["init", "-b", "main"], cwd=root, timeout=60)
    except Exception:
        # Older Git versions do not support -b; fall back to the default branch.
        run_git(["init"], cwd=root, timeout=60)


def _initial_commit(root: Path) -> bool:
    identity = ["-c", "user.name=AgentMesh", "-c", "user.email=agentmesh@localhost"]
    try:
        run_git([*identity, "add", "-A"], cwd=root, timeout=60, check=False)
        proc = run_git([*identity, "commit", "--allow-empty", "-m",
                        "chore: initialize AgentMesh workspace"],
                       cwd=root, timeout=60, check=False)
        return proc.returncode == 0
    except Exception:
        return False


__all__ = ["initialize_project"]
