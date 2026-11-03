"""Deterministic file-change observation (polling snapshot based)."""

from __future__ import annotations

import fnmatch
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from ..util import sha256_file


DEFAULT_EXCLUDES = {
    ".git", ".agentloop", ".agentloop-task", "node_modules", ".venv", "venv",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next", "dist",
    "build", "target", ".idea", ".vscode", ".DS_Store",
}


def is_excluded(rel: str, patterns: Iterable[str] = ()) -> bool:
    parts = rel.replace("\\", "/").split("/")
    if any(part in DEFAULT_EXCLUDES for part in parts):
        return True
    for pattern in patterns:
        if fnmatch.fnmatch(rel, pattern) or any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True
    return False


@dataclass
class FileState:
    path: str
    size: int
    mtime_ns: int
    digest: str = ""

    def to_dict(self) -> dict:
        return {"path": self.path, "size": self.size, "mtime_ns": self.mtime_ns, "digest": self.digest}


@dataclass
class FileChange:
    kind: str  # created / modified / deleted
    path: str
    before: FileState | None = None
    after: FileState | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "path": self.path,
            "before_hash": self.before.digest if self.before else "",
            "after_hash": self.after.digest if self.after else "",
        }


@dataclass
class Snapshot:
    root: str
    files: dict[str, FileState] = field(default_factory=dict)
    taken_at: float = field(default_factory=time.time)

    def paths(self) -> list[str]:
        return sorted(self.files)


def snapshot_tree(root: str | os.PathLike[str], *, excludes: Iterable[str] = (),
                  max_files: int = 50000, with_digest: bool = False) -> Snapshot:
    base = Path(root)
    snap = Snapshot(root=str(base))
    count = 0
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = os.path.relpath(dirpath, base).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        dirnames[:] = sorted(
            d for d in dirnames
            if not is_excluded((rel_dir + "/" + d).strip("/"), excludes)
        )
        for name in sorted(filenames):
            rel = (rel_dir + "/" + name).strip("/")
            if is_excluded(rel, excludes):
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            digest = sha256_file(full) if with_digest else ""
            snap.files[rel] = FileState(rel, st.st_size, st.st_mtime_ns, digest)
            count += 1
            if count >= max_files:
                return snap
    return snap


def diff_snapshots(before: Snapshot, after: Snapshot, *, verify_digest: bool = False) -> list[FileChange]:
    changes: list[FileChange] = []
    for path, a in after.files.items():
        b = before.files.get(path)
        if b is None:
            changes.append(FileChange("created", path, None, a))
        elif b.size != a.size or b.mtime_ns != a.mtime_ns:
            if verify_digest and a.digest and b.digest and a.digest == b.digest:
                continue
            changes.append(FileChange("modified", path, b, a))
    for path, b in before.files.items():
        if path not in after.files:
            changes.append(FileChange("deleted", path, b, None))
    changes.sort(key=lambda c: (c.path, c.kind))
    return changes


class WorkspaceMonitor:
    """Polling watcher. Deterministic, testable, works on Windows without pywin32."""

    def __init__(self, root: str | os.PathLike[str], *, interval: float = 3.0,
                 excludes: Iterable[str] = (), on_change: Callable[[FileChange], None] | None = None):
        self.root = str(root)
        self.interval = max(0.2, float(interval))
        self.excludes = tuple(excludes)
        self.on_change = on_change
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot = snapshot_tree(self.root, excludes=self.excludes, with_digest=True)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="agentloop-workspace-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def poll_once(self) -> list[FileChange]:
        after = snapshot_tree(self.root, excludes=self.excludes, with_digest=True)
        changes = diff_snapshots(self._snapshot, after, verify_digest=True)
        self._snapshot = after
        if self.on_change:
            for change in changes:
                self.on_change(change)
        return changes

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll_once()
            except Exception:
                # A monitor must never take down the control plane.
                continue

    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot


def hash_changed_files(root: str | os.PathLike[str], files: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    base = Path(root)
    for rel in files:
        path = base / rel
        if path.is_file():
            out[rel] = sha256_file(path)
        else:
            out[rel] = ""
    return out
