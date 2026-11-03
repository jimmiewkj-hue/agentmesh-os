"""Git operations: metadata, worktrees, patches, apply/revert, status."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..errors import GitOperationError
from ..util import ensure_dir, sha256_file


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def run_git(args: list[str], *, cwd: str | os.PathLike[str], check: bool = True,
            timeout: int = 60, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("GIT_PAGER", "cat")
    env.setdefault("LC_ALL", "C.UTF-8")
    if env_extra:
        env.update(env_extra)
    cmd = ["git", *args]
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, timeout=timeout, env=env,
            text=False,
        )
    except FileNotFoundError as exc:
        raise GitOperationError("未找到 git 可执行文件，请先安装 Git") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitOperationError(f"git 命令超时: {' '.join(cmd)}") from exc
    stdout = _decode(proc.stdout or b"")
    stderr = _decode(proc.stderr or b"")
    if check and proc.returncode != 0:
        raise GitOperationError(
            f"git 命令失败 ({proc.returncode}): {' '.join(cmd)}\n{stderr.strip()}",
            details={"command": cmd, "stdout": stdout, "stderr": stderr},
        )
    proc.stdout_text = stdout  # type: ignore[attr-defined]
    proc.stderr_text = stderr  # type: ignore[attr-defined]
    return proc


def is_git_repo(path: str | os.PathLike[str]) -> bool:
    proc = run_git(["rev-parse", "--is-inside-work-tree"], cwd=path, check=False)
    return proc.returncode == 0 and (proc.stdout_text or "").strip() == "true"  # type: ignore[attr-defined]


def detect_repo(path: str | os.PathLike[str]) -> dict[str, Any]:
    if not is_git_repo(path):
        return {"is_git": False, "toplevel": "", "branch": "", "commit": "", "remote": ""}
    toplevel = run_git(["rev-parse", "--show-toplevel"], cwd=path).stdout_text.strip()  # type: ignore[attr-defined]
    branch_proc = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path, check=False)
    branch = branch_proc.stdout_text.strip() if branch_proc.returncode == 0 else ""  # type: ignore[attr-defined]
    commit_proc = run_git(["rev-parse", "HEAD"], cwd=path, check=False)
    commit = commit_proc.stdout_text.strip() if commit_proc.returncode == 0 else ""  # type: ignore[attr-defined]
    if branch.upper() == "HEAD":
        branch = ""
    remote = run_git(["remote", "get-url", "origin"], cwd=path, check=False).stdout_text.strip()  # type: ignore[attr-defined]
    return {
        "is_git": True, "toplevel": toplevel, "branch": branch,
        "commit": commit, "remote": remote,
    }


def git_dir(path: str | os.PathLike[str]) -> str:
    return run_git(["rev-parse", "--git-dir"], cwd=path).stdout_text.strip()  # type: ignore[attr-defined]


def add_info_exclude(root: str | os.PathLike[str], patterns: Iterable[str]) -> None:
    """Add patterns to .git/info/exclude so hub data never pollutes project status."""
    repo = Path(root)
    git_dir_path = Path(git_dir(root))
    if not git_dir_path.is_absolute():
        git_dir_path = repo / git_dir_path
    exclude = git_dir_path / "info" / "exclude"
    ensure_dir(exclude.parent)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    lines = set(existing.splitlines())
    changed = False
    for pattern in patterns:
        if pattern not in lines:
            lines.add(pattern)
            changed = True
    if changed:
        exclude.write_text("\n".join(sorted(lines)).rstrip() + "\n", encoding="utf-8")


def status_porcelain(root: str | os.PathLike[str], *, pathspec: list[str] | None = None) -> list[dict[str, str]]:
    args = ["status", "--porcelain", "-z", "--untracked-files=all"]
    if pathspec:
        args += ["--", *pathspec]
    proc = run_git(args, cwd=root)
    raw = proc.stdout_text  # type: ignore[attr-defined]
    entries: list[dict[str, str]] = []
    parts = raw.split("\0")
    i = 0
    while i < len(parts):
        item = parts[i]
        if not item:
            i += 1
            continue
        status = item[:2]
        path = item[3:]
        if status.startswith("R") or status.startswith("C"):
            # porcelain -z puts the new path first and the old path in the next token
            old_path = parts[i + 1] if i + 1 < len(parts) else ""
            entries.append({"status": status, "path": path, "old_path": old_path})
            i += 2
            continue
        entries.append({"status": status, "path": path, "old_path": ""})
        i += 1
    return entries


def is_clean(root: str | os.PathLike[str], *, pathspec: list[str] | None = None) -> bool:
    return not status_porcelain(root, pathspec=pathspec)


def _git_path(path: str) -> str:
    return path.replace("\\", "/")


@dataclass
class Worktree:
    path: str
    base_commit: str
    branch: str = ""
    detached: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "base_commit": self.base_commit,
                "branch": self.branch, "detached": self.detached}


def create_worktree(root: str | os.PathLike[str], target: str | os.PathLike[str],
                    base_commit: str, *, branch: str = "") -> Worktree:
    target_path = Path(target)
    if target_path.exists():
        raise GitOperationError(f"Worktree 目标已存在: {target_path}")
    ensure_dir(target_path.parent)
    args = ["worktree", "add"]
    if branch:
        args += ["-b", branch, str(target_path), base_commit]
        wt = Worktree(str(target_path), base_commit, branch=branch, detached=False)
    else:
        args += ["--detach", str(target_path), base_commit]
        wt = Worktree(str(target_path), base_commit)
    run_git(args, cwd=root, timeout=120)
    return wt


def remove_worktree(root: str | os.PathLike[str], target: str | os.PathLike[str], *, force: bool = True) -> None:
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(target))
    proc = run_git(args, cwd=root, check=False, timeout=90)
    if proc.returncode != 0 and Path(target).exists():
        raise GitOperationError(
            f"移除 Worktree 失败: {proc.stderr_text.strip()}",  # type: ignore[attr-defined]
        )


def prune_worktrees(root: str | os.PathLike[str]) -> None:
    run_git(["worktree", "prune"], cwd=root, check=False)


EXCLUDE_PATHSPEC = ":!:.agentloop-task"


def changed_paths(worktree: str | os.PathLike[str], *, exclude_prefixes: Iterable[str] = (".agentloop-task",)) -> list[dict[str, str]]:
    entries = status_porcelain(worktree)
    out = []
    for entry in entries:
        path = _git_path(entry["path"])
        if any(path == p or path.startswith(p.rstrip("/") + "/") for p in exclude_prefixes):
            continue
        out.append(entry)
    return out


def generate_patch(worktree: str | os.PathLike[str], dst: str | os.PathLike[str],
                   *, exclude_prefixes: Iterable[str] = (".agentloop-task",)) -> str:
    """Generate a binary-safe patch for all worktree changes (including untracked)."""
    wt = str(worktree)
    exclude = [f":!:{p}" for p in exclude_prefixes]
    # Intent-to-add so new files appear in the diff. Ignore failure on empty matches.
    run_git(["add", "-N", "--", ".", *exclude], cwd=wt, check=False, timeout=120)
    proc = run_git(["diff", "--binary", "HEAD", "--", ".", *exclude], cwd=wt, check=False, timeout=120)
    patch = proc.stdout_text  # type: ignore[attr-defined]
    ensure_dir(Path(dst).parent)
    Path(dst).write_text(patch, encoding="utf-8", newline="\n")
    return str(dst)


def patch_hash(path: str | os.PathLike[str]) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    return sha256_file(p)


def patch_files(patch_path: str | os.PathLike[str]) -> list[str]:
    """Extract affected paths from a git patch (a/… and b/… headers)."""
    text = Path(patch_path).read_text(encoding="utf-8", errors="replace")
    files: list[str] = []
    for line in text.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            if m:
                files.extend([m.group(1), m.group(2)])
        elif line.startswith("--- a/") or line.startswith("+++ b/"):
            files.append(line[6:])
        elif line.startswith("rename from ") or line.startswith("rename to "):
            files.append(line.split(" ", 3)[3])
    return sorted({f for f in files if f and f != "/dev/null"})


def apply_patch(root: str | os.PathLike[str], patch_path: str | os.PathLike[str],
                *, check_only: bool = False, three_way: bool = True,
                include: str | None = None) -> tuple[bool, str]:
    args = ["apply", "--whitespace=nowarn"]
    if check_only:
        args.append("--check")
    if three_way:
        args.append("--3way")
    if include:
        args.append(f"--include={include}")
    args.append(str(patch_path))
    proc = run_git(args, cwd=root, check=False, timeout=120)
    return proc.returncode == 0, (proc.stderr_text or proc.stdout_text).strip()  # type: ignore[attr-defined]


def reverse_patch(root: str | os.PathLike[str], patch_path: str | os.PathLike[str],
                  *, check_only: bool = False, include: str | None = None) -> tuple[bool, str]:
    args = ["apply", "-R", "--whitespace=nowarn"]
    if check_only:
        args.append("--check")
    if include:
        args.append(f"--include={include}")
    args.append(str(patch_path))
    proc = run_git(args, cwd=root, check=False, timeout=120)
    return proc.returncode == 0, (proc.stderr_text or proc.stdout_text).strip()  # type: ignore[attr-defined]


def file_fingerprint(path: str | os.PathLike[str]) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    if p.is_dir():
        return ""
    return sha256_file(p)


def commit_all(root: str | os.PathLike[str], message: str) -> str:
    run_git(["add", "-A"], cwd=root, timeout=120)
    run_git(["commit", "-m", message], cwd=root, timeout=120)
    return run_git(["rev-parse", "HEAD"], cwd=root).stdout_text.strip()  # type: ignore[attr-defined]


def restore_worktree(root: str | os.PathLike[str]) -> None:
    run_git(["checkout", "--", "."], cwd=root, timeout=120)
    run_git(["reset", "--hard", "HEAD"], cwd=root, timeout=120)
    entries = status_porcelain(root)
    for entry in entries:
        if entry["status"] == "??":
            p = Path(root) / entry["path"]
            if p.is_file():
                p.unlink(missing_ok=True)


def base_commit(root: str | os.PathLike[str]) -> str:
    return run_git(["rev-parse", "HEAD"], cwd=root).stdout_text.strip()  # type: ignore[attr-defined]


def branch(root: str | os.PathLike[str]) -> str:
    return run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root, check=False).stdout_text.strip()  # type: ignore[attr-defined]


def list_worktrees(root: str | os.PathLike[str]) -> list[dict[str, str]]:
    proc = run_git(["worktree", "list", "--porcelain"], cwd=root, check=False)
    text = proc.stdout_text  # type: ignore[attr-defined]
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        entries.append(current)
    return entries
