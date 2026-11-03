"""Workspace package."""

from .changes import FileChange, Snapshot, WorkspaceMonitor, diff_snapshots, snapshot_tree
from .gitops import (
    Worktree, add_info_exclude, apply_patch, base_commit, branch, changed_paths,
    commit_all, create_worktree, detect_repo, generate_patch, is_git_repo, is_clean,
    patch_files, remove_worktree, reverse_patch, run_git, status_porcelain, list_worktrees,
)
from .identity import build_project, canonical_path, detect_tech_stack, normalize_root
from .init import initialize_project
from .leases import LeaseManager
from .manager import WorkspaceManager

__all__ = [
    "Worktree", "add_info_exclude", "apply_patch", "base_commit", "branch", "changed_paths",
    "commit_all", "create_worktree", "detect_repo", "generate_patch", "is_git_repo",
    "is_clean", "patch_files", "remove_worktree", "reverse_patch", "run_git",
    "status_porcelain", "build_project", "canonical_path", "detect_tech_stack",
    "normalize_root", "LeaseManager", "FileChange", "Snapshot", "WorkspaceMonitor",
    "diff_snapshots", "snapshot_tree", "list_worktrees", "initialize_project",
    "WorkspaceManager",
]
