"""Filesystem browsing for the Web console's working-directory picker.

DSH's web build exposes a *directory-picker capability* with two shapes: a
native ``dialog.showOpenDialog({properties:["openDirectory","createDirectory"]})``
on desktop, and a **browse** capability (``list`` / ``createDirectory``) for the
browser build.  A browser cannot be handed an absolute path by
``showDirectoryPicker()`` — it only returns an opaque handle — and a
server-side modal dialog would block a headless server, so AgentMesh implements
the same *browse* capability the web build uses:

* ``GET  /api/fs/list?path=``      → one level + its ancestry (breadcrumb-ready)
* ``POST /api/fs/mkdir``           → create one child directory
* ``GET  /api/workspaces/recent``  → recently opened project roots

Security boundary: these endpoints reveal the local filesystem, so the HTTP
layer refuses them unless the caller is loopback. Paths are normalised and must
be absolute; nothing outside the caller's own machine is served.
"""

from __future__ import annotations

import os
import re
import string
from pathlib import Path
from typing import Any

from ..config import default_home
from ..errors import ValidationError
from ..util import now_iso, read_json, write_json

MAX_ENTRIES = 400
RECENT_LIMIT = 12
_INVALID_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def is_loopback_address(peer: str) -> bool:
    """Whether a socket peer is the local machine.

    Directory listing is a local-only capability: the console may be started
    with ``--host 0.0.0.0`` for LAN use, and that must not expose the disk.
    """
    host = str(peer or "").strip()
    return host in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1") \
        or host.startswith("127.")


# ---------------------------------------------------------------------------
# Roots / ancestry
# ---------------------------------------------------------------------------

def roots() -> list[dict[str, str]]:
    """Filesystem roots: one per drive on Windows, ``/`` elsewhere."""
    if os.name != "nt":
        return [{"name": "/", "path": "/"}]
    out: list[dict[str, str]] = []
    for letter in string.ascii_uppercase:
        candidate = f"{letter}:\\"
        try:
            if Path(candidate).exists():
                out.append({"name": candidate.rstrip("\\"), "path": candidate})
        except OSError:
            continue
    return out


def ancestry(path: Path) -> list[dict[str, str]]:
    """Root → … → path, so the UI can render a breadcrumb without guessing."""
    chain: list[Path] = []
    current = path
    while True:
        chain.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    chain.reverse()
    crumbs: list[dict[str, str]] = []
    for item in chain:
        name = item.name or str(item)
        crumbs.append({"name": name, "path": str(item)})
    return crumbs


def _normalise(path: str) -> Path:
    raw = str(path or "").strip()
    if not raw:
        raise ValidationError("缺少目录路径 path")
    expanded = os.path.expandvars(os.path.expanduser(raw))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        raise ValidationError(f"必须是绝对路径: {raw}")
    try:
        return candidate.resolve()
    except OSError as exc:  # broken symlink / permission on the parent
        raise ValidationError(f"路径无法解析: {raw} ({exc})") from exc


def _entry(path: Path, *, is_dir: bool) -> dict[str, Any]:
    name = path.name or str(path)
    item: dict[str, Any] = {
        "name": name,
        "path": str(path),
        "is_dir": bool(is_dir),
        "hidden": name.startswith("."),
    }
    if not is_dir:
        try:
            item["size"] = path.stat().st_size
        except OSError:
            item["size"] = 0
    return item


# ---------------------------------------------------------------------------
# Listing / mkdir
# ---------------------------------------------------------------------------

def list_level(path: str = "") -> dict[str, Any]:
    """One directory level plus the data a breadcrumb needs.

    A missing path is **not** an HTTP error: the picker opens at a stale
    location all the time (a deleted folder, an unplugged drive), and the UI
    must be able to show why instead of dying on a 400.
    """
    if not str(path or "").strip():
        return {
            "path": "", "parent": "", "ancestry": [], "entries": [],
            "roots": roots(), "home": str(Path.home()), "cwd": str(Path.cwd()),
            "truncated": False, "error": "",
        }
    target = _normalise(path)
    if not target.exists():
        return {
            "path": str(target), "parent": str(target.parent), "ancestry": ancestry(target.parent),
            "entries": [], "roots": roots(), "home": str(Path.home()),
            "cwd": str(Path.cwd()), "truncated": False,
            "error": "目录不存在",
        }
    if not target.is_dir():
        raise ValidationError(f"不是目录: {target}")
    entries: list[dict[str, Any]] = []
    truncated = False
    try:
        with os.scandir(target) as iterator:
            for raw in iterator:
                try:
                    is_dir = raw.is_dir(follow_symlinks=False)
                except OSError:
                    is_dir = False
                entries.append(_entry(Path(raw.path), is_dir=is_dir))
                if len(entries) >= MAX_ENTRIES:
                    truncated = True
                    break
    except PermissionError:
        return {
            "path": str(target), "parent": str(target.parent), "ancestry": ancestry(target),
            "entries": [], "roots": roots(), "home": str(Path.home()),
            "cwd": str(Path.cwd()), "truncated": False, "error": "没有访问权限",
        }
    except OSError as exc:
        return {
            "path": str(target), "parent": str(target.parent), "ancestry": ancestry(target),
            "entries": [], "roots": roots(), "home": str(Path.home()),
            "cwd": str(Path.cwd()), "truncated": False, "error": str(exc)[:200],
        }
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].casefold()))
    return {
        "path": str(target),
        "parent": str(target.parent) if target.parent != target else "",
        "ancestry": ancestry(target),
        "entries": entries,
        "roots": roots(),
        "home": str(Path.home()),
        "cwd": str(Path.cwd()),
        "truncated": truncated,
        "error": "",
    }


def create_directory(path: str, name: str) -> dict[str, Any]:
    """Create one child directory inside an existing parent."""
    target = _normalise(path)
    if not target.is_dir():
        raise ValidationError(f"父目录不存在: {target}")
    clean = str(name or "").strip()
    if not clean:
        raise ValidationError("文件夹名不能为空")
    if clean in (".", "..") or _INVALID_NAME.search(clean):
        raise ValidationError(f"文件夹名非法: {clean}")
    child = target / clean
    try:
        child.mkdir()
    except FileExistsError as exc:
        raise ValidationError(f"已存在同名文件或目录: {clean}") from exc
    except OSError as exc:
        raise ValidationError(f"创建失败: {exc}") from exc
    entry = _entry(child, is_dir=True)
    entry["parent"] = str(target)
    return entry


# ---------------------------------------------------------------------------
# Recent projects
# ---------------------------------------------------------------------------

def _recent_path() -> Path:
    return default_home() / "recent-projects.json"


def recent_projects() -> list[dict[str, Any]]:
    data = read_json(_recent_path(), []) or []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if not path:
            continue
        out.append({
            "path": path,
            "name": str(item.get("name") or Path(path).name or path),
            "opened_at": str(item.get("opened_at") or ""),
            "exists": Path(path).is_dir(),
        })
    return out


def remember_project(path: str, name: str = "") -> dict[str, Any]:
    """Record a project root as recently opened (best-effort, never raises)."""
    raw = str(path or "").strip()
    if not raw:
        return {"remembered": False}
    try:
        resolved = str(Path(raw).expanduser().resolve())
    except OSError:
        resolved = raw
    items = [item for item in recent_projects() if item["path"] != resolved]
    items.insert(0, {
        "path": resolved,
        "name": name or Path(resolved).name or resolved,
        "opened_at": now_iso(),
    })
    try:
        write_json(_recent_path(), items[:RECENT_LIMIT], indent=2)
    except OSError:
        return {"remembered": False, "path": resolved}
    return {"remembered": True, "path": resolved, "total": min(len(items), RECENT_LIMIT)}


__all__ = [
    "is_loopback_address", "roots", "ancestry", "list_level", "create_directory",
    "recent_projects", "remember_project",
]
