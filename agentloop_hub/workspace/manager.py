"""Workspace manager: one Web console can initialize and switch projects."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from ..errors import ConfigError
from .init import initialize_project


class WorkspaceManager:
    def __init__(self, project_root: str | Path | None = None, *,
                 auto_discover: bool = True):
        self._lock = threading.RLock()
        self._hub: Any = None
        self._root: Path | None = None
        self._last_error = ""
        self.auto_discover = auto_discover
        if project_root:
            try:
                self.open(project_root, create=False, git_init=False)
            except Exception as exc:  # keep the server alive so users can init via UI
                self._last_error = str(exc)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def hub(self) -> Any:
        return self.require()

    def require(self) -> Any:
        with self._lock:
            if self._hub is None:
                raise ConfigError(
                    "尚未打开项目。请先在控制台初始化或打开一个 Git 项目目录。",
                    details={"last_error": self._last_error},
                )
            return self._hub

    def open(self, path: str | Path, *, create: bool = False,
             git_init: bool = False, name: str = "") -> dict[str, Any]:
        root = Path(path).expanduser().resolve()
        if not root.exists() and not create:
            raise ConfigError(f"项目目录不存在: {root}（可勾选“创建目录”）")
        info = initialize_project(root, name=name, git_init=git_init or create)
        with self._lock:
            if self._hub is not None and self._root == root:
                self._last_error = ""
                return {**info, "project": self._hub.overview()["project"],
                        "switched": False}
        from ..hub import Hub
        new_hub = Hub(root, auto_discover=self.auto_discover)
        with self._lock:
            old = self._hub
            self._hub = new_hub
            self._root = root
            self._last_error = ""
            if old is not None and old is not new_hub:
                try:
                    old.close()
                except Exception:
                    pass
            return {**info, "project": new_hub.overview()["project"], "switched": True}

    def close(self) -> None:
        with self._lock:
            if self._hub is not None:
                try:
                    self._hub.close()
                except Exception:
                    pass
            self._hub = None
            self._root = None

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def info(self) -> dict[str, Any]:
        with self._lock:
            hub = self._hub
            root = self._root
        if hub is None:
            return {
                "open": False, "root": str(root) if root else "",
                "error": self._last_error,
                "hint": "输入项目路径并点击“初始化/打开项目”。",
            }
        project = hub.project.to_dict()
        return {
            "open": True, "root": project["root"], "error": "",
            "project": project,
        }


__all__ = ["WorkspaceManager"]
