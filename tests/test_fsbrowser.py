"""Local directory browsing for the Web console's working-directory picker."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from agentloop_hub.control import fsbrowser
from agentloop_hub.control.fsbrowser import (
    create_directory, is_loopback_address, list_level, recent_projects,
    remember_project, roots,
)
from agentloop_hub.errors import ValidationError


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "src" / "nested").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / ".hidden").mkdir()
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    (root / "src" / "calc.py").write_text("x = 1\n", encoding="utf-8")
    return root


def test_roots_are_real_existing_locations():
    found = roots()
    assert found, "至少应有一个根"
    for item in found:
        assert Path(item["path"]).exists()
        assert item["name"]


def test_empty_path_returns_roots_and_home():
    data = list_level("")
    assert data["path"] == ""
    assert data["entries"] == []
    assert data["roots"] and data["home"] and data["cwd"]
    assert data["error"] == ""


def test_listing_puts_directories_first_and_marks_files(tree: Path):
    data = list_level(str(tree))
    assert data["error"] == ""
    names = [entry["name"] for entry in data["entries"]]
    assert {"src", "docs", ".hidden", "README.md"} <= set(names)
    assert names.index("src") < names.index("README.md")
    readme = next(e for e in data["entries"] if e["name"] == "README.md")
    assert readme["is_dir"] is False and readme["size"] > 0
    hidden = next(e for e in data["entries"] if e["name"] == ".hidden")
    assert hidden["hidden"] is True


def test_ancestry_is_root_to_current(tree: Path):
    data = list_level(str(tree / "src"))
    assert [crumb["path"] for crumb in data["ancestry"]] == [
        str(Path(crumb["path"])) for crumb in data["ancestry"]
    ]
    assert data["ancestry"][-1]["path"] == str(tree / "src")
    assert data["ancestry"][0]["path"] == str(Path(str(tree / "src")).parents[-1])
    assert data["parent"] == str(tree)


def test_a_missing_directory_is_reported_not_raised(tmp_path: Path):
    data = list_level(str(tmp_path / "does-not-exist"))
    assert data["entries"] == []
    assert data["error"] == "目录不存在"
    # the breadcrumb still works from the nearest existing parent
    assert data["ancestry"]


def test_relative_paths_are_rejected():
    with pytest.raises(ValidationError):
        list_level("relative/dir")


def test_a_file_is_not_a_directory(tree: Path):
    with pytest.raises(ValidationError):
        list_level(str(tree / "README.md"))


def test_create_directory_returns_the_new_entry(tree: Path):
    entry = create_directory(str(tree), "new-project")
    assert entry["is_dir"] is True
    assert Path(entry["path"]).is_dir()
    assert entry["parent"] == str(tree)


@pytest.mark.parametrize("name", ["", "   ", ".", "..", "a/b", "a\\b", "a:b", "a?b"])
def test_invalid_directory_names_are_rejected(tree: Path, name: str):
    with pytest.raises(ValidationError):
        create_directory(str(tree), name)


def test_existing_name_is_rejected(tree: Path):
    with pytest.raises(ValidationError):
        create_directory(str(tree), "src")


def test_create_inside_a_missing_parent_is_rejected(tmp_path: Path):
    with pytest.raises(ValidationError):
        create_directory(str(tmp_path / "nope"), "x")


def test_recent_projects_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENTLOOP_HOME", str(tmp_path / "home"))
    first = tmp_path / "alpha"
    second = tmp_path / "beta"
    first.mkdir()
    second.mkdir()
    remember_project(str(first), "alpha")
    remember_project(str(second))
    remember_project(str(first), "alpha-again")  # dedupe, newest first
    rows = recent_projects()
    assert [row["path"] for row in rows] == [str(first.resolve()), str(second.resolve())]
    assert rows[0]["name"] == "alpha-again"
    assert all(row["exists"] for row in rows)


def test_recent_projects_are_capped(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENTLOOP_HOME", str(tmp_path / "home"))
    for index in range(20):
        target = tmp_path / f"p{index}"
        target.mkdir()
        remember_project(str(target))
    assert len(recent_projects()) == fsbrowser.RECENT_LIMIT


def test_recent_list_survives_a_corrupt_file(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENTLOOP_HOME", str(home))
    (home / "recent-projects.json").write_text("{not json", encoding="utf-8")
    assert recent_projects() == []


def test_loopback_detection():
    for peer in ("127.0.0.1", "127.1.2.3", "::1", "localhost", "::ffff:127.0.0.1"):
        assert is_loopback_address(peer) is True
    for peer in ("192.168.1.5", "10.0.0.2", "0.0.0.0", "", "example.com"):
        assert is_loopback_address(peer) is False


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

@pytest.fixture()
def server(tmp_path: Path):
    from conftest import make_project
    from agentloop_hub.api.server import JobManager, _make_handler

    repo, hub, _ = make_project(tmp_path)
    handler = _make_handler(hub, JobManager(), None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], repo
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(port: int, path: str, body: dict) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def test_http_list_works_before_a_workspace_is_opened(server):
    port, repo = server
    data = _get(port, "/api/fs/list")  # no path -> roots
    assert data["roots"]
    data = _get(port, "/api/fs/list?" + urllib.parse.urlencode({"path": str(repo)}))
    assert data["path"] == str(repo.resolve())
    assert any(entry["name"] == "README.md" for entry in data["entries"])


def test_http_mkdir_and_recent(server, tmp_path: Path, monkeypatch):
    port, repo = server
    monkeypatch.setenv("AGENTLOOP_HOME", str(tmp_path / "home"))
    created = _post(port, "/api/fs/mkdir", {"path": str(repo), "name": "picked"})
    assert Path(created["path"]).is_dir()
    listed = _get(port, "/api/fs/list?" + urllib.parse.urlencode({"path": str(repo)}))
    assert "picked" in [entry["name"] for entry in listed["entries"]]


def test_http_mkdir_rejects_bad_names(server):
    port, repo = server
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/fs/mkdir",
        data=json.dumps({"path": str(repo), "name": "../escape"}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=10)
    assert exc.value.code == 400
    body = json.loads(exc.value.read().decode("utf-8"))
    assert body["error"]["code"] == "validation_error"
