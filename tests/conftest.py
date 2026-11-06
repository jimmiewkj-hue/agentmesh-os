"""Pytest fixtures for the project's own test suite.

Helpers live in ``hub_test_support`` (not in this file) so that collecting
``tests`` and ``verification`` together cannot alias two same-named
``conftest`` modules.  Test modules should import helpers from
``hub_test_support``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hub_test_support import init_repo  # noqa: E402


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")
