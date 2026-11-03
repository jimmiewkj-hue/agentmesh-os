"""Workspace identity, git metadata and technology-stack detection."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ..models import Project
from ..util import sha1_hex


def normalize_root(path: str | os.PathLike[str]) -> str:
    p = Path(path).expanduser().resolve()
    return str(p)


def canonical_path(path: str | os.PathLike[str]) -> str:
    raw = normalize_root(path)
    if os.name == "nt":
        raw = raw.lower()
    return raw


def workspace_id_for(root: str) -> str:
    return "ws_" + sha1_hex(canonical_path(root))[:16]


def repository_id_for(root: str, git_remote: str = "") -> str:
    identity = git_remote.strip().lower() or canonical_path(root)
    return "repo_" + sha1_hex(identity)[:16]


def worktree_id_for(root: str) -> str:
    return "wt_" + sha1_hex(canonical_path(root))[:16]


TECH_STACK_MARKERS: dict[str, str] = {
    "package.json": "node",
    "pnpm-lock.yaml": "node-pnpm",
    "yarn.lock": "node-yarn",
    "package-lock.json": "node-npm",
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "setup.py": "python",
    "Pipfile": "python",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "pom.xml": "java-maven",
    "build.gradle": "java-gradle",
    "build.gradle.kts": "java-gradle",
    "CMakeLists.txt": "cmake",
    "Makefile": "make",
    "*.csproj": "dotnet",
    "*.sln": "dotnet",
    "Gemfile": "ruby",
    "composer.json": "php",
    "pubspec.yaml": "dart",
}


def detect_tech_stack(root: str) -> list[str]:
    found: list[str] = []
    base = Path(root)
    for pattern, name in TECH_STACK_MARKERS.items():
        if pattern.startswith("*"):
            if list(base.glob(pattern)):
                found.append(name)
        elif (base / pattern).exists():
            found.append(name)
    return sorted(set(found))


def detect_commands(root: str) -> tuple[str, str]:
    """Return (build_command, test_command) using deterministic, non-guessing rules."""
    base = Path(root)
    build = ""
    test = ""

    pkg = base / "package.json"
    if pkg.exists():
        try:
            import json
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {}) or {}
        except (OSError, ValueError):
            scripts = {}
        if "build" in scripts:
            build = "npm run build"
        if "test" in scripts:
            test = "npm test"
        elif "vitest" in str(scripts.get("test", "")):
            test = "npm test"
    if not build:
        for candidate in ("pnpm-lock.yaml", "yarn.lock"):
            if (base / candidate).exists():
                build = "pnpm build" if candidate.startswith("pnpm") else "yarn build"
                break
    if not build:
        if (base / "pyproject.toml").exists() or (base / "setup.py").exists():
            build = "python -m build" if (base / "pyproject.toml").exists() else ""
        elif (base / "go.mod").exists():
            build = "go build ./..."
        elif (base / "Cargo.toml").exists():
            build = "cargo build"
        elif (base / "pom.xml").exists():
            build = "mvn -q -DskipTests package"
        elif (base / "build.gradle").exists() or (base / "build.gradle.kts").exists():
            build = "gradle build -x test"
        elif list(base.glob("*.csproj")):
            build = "dotnet build"
        elif (base / "CMakeLists.txt").exists():
            build = "cmake --build build"
    if not test:
        if (base / "pyproject.toml").exists() or (base / "pytest.ini").exists() or (base / "tests").exists():
            test = "python -m pytest -q"
        elif (base / "go.mod").exists():
            test = "go test ./..."
        elif (base / "Cargo.toml").exists():
            test = "cargo test"
        elif (base / "pom.xml").exists():
            test = "mvn -q test"
        elif (base / "build.gradle").exists() or (base / "build.gradle.kts").exists():
            test = "gradle test"
        elif list(base.glob("*.csproj")):
            test = "dotnet test"
    return build, test


def detect_project_name(root: str) -> str:
    base = Path(root)
    for name in ("package.json", "pyproject.toml", "Cargo.toml", "go.mod"):
        path = base / name
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if name == "package.json":
            m = re.search(r'"name"\s*:\s*"([^"]+)"', text)
        elif name == "pyproject.toml":
            m = re.search(r'^\s*name\s*=\s*"([^"]+)"', text, re.M)
        elif name == "Cargo.toml":
            m = re.search(r'^\s*name\s*=\s*"([^"]+)"', text, re.M)
        else:
            m = re.search(r'^module\s+(\S+)', text, re.M)
        if m:
            return m.group(1)
    readme = base / "README.md"
    if readme.exists():
        try:
            first = readme.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            first = []
        for line in first[:5]:
            if line.startswith("# "):
                title = line[2:].strip()
                if title:
                    return title
    return base.name or "project"


def build_project(root: str, *, project_id: str, git_info: dict[str, Any]) -> Project:
    tech = detect_tech_stack(root)
    build_cmd, test_cmd = detect_commands(root)
    return Project(
        project_id=project_id,
        name=detect_project_name(root),
        root=normalize_root(root),
        workspace_id=workspace_id_for(root),
        repository_id=repository_id_for(root, str(git_info.get("remote", ""))),
        worktree_id=worktree_id_for(root),
        git_remote=str(git_info.get("remote", "")),
        branch=str(git_info.get("branch", "")),
        commit=str(git_info.get("commit", "")),
        tech_stack=tech,
        build_command=build_cmd,
        test_command=test_cmd,
        extra={"git_toplevel": git_info.get("toplevel", ""), "is_git": bool(git_info.get("is_git"))},
    )
