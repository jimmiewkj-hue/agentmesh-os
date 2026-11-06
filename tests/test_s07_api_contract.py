"""S07: draft revisions, durable jobs and HTTP contract for the web console."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from hub_test_support import make_project

from agentloop_hub.errors import ConflictError


def test_chain_draft_revision_rejects_stale_writes(tmp_path: Path):
    repo, hub, _ = make_project(tmp_path)
    first = hub.save_chain_draft({
        "goal": "goal", "stages": [{"agent_id": "mock_worker", "model": "mock-model",
                                     "type": "analysis"}],
        "base_revision": 0,
    })
    assert first["revision"] == 1
    second = hub.save_chain_draft({
        "goal": "goal 2", "stages": [{"agent_id": "mock_worker", "model": "mock-model",
                                       "type": "analysis"}],
        "base_revision": 1,
    })
    assert second["revision"] == 2
    with pytest.raises(ConflictError):
        hub.save_chain_draft({
            "goal": "stale", "stages": [], "base_revision": 1,
        })
    current = hub.chain_draft()
    assert current["goal"] == "goal 2"
    assert current["revision"] == 2
    hub.close()


def test_job_manager_persists_and_marks_interrupted_jobs(tmp_path: Path):
    from agentloop_hub.api.server import JobManager

    path = tmp_path / "jobs.json"
    jobs = JobManager(path)
    job_id = jobs.submit("plan", lambda: {"status": "pending_approval"})
    for _ in range(100):
        job = jobs.get(job_id)
        if job and job["status"] != "running":
            break
        time.sleep(0.02)
    assert job["status"] == "completed"
    assert job["business_status"] == "pending_approval"
    # Re-open the same store: the completed job is still visible.
    reopened = JobManager(path)
    assert reopened.get(job_id)["status"] == "completed"

    data = json.loads(path.read_text(encoding="utf-8"))
    data["jobs"][0]["status"] = "running"
    path.write_text(json.dumps(data), encoding="utf-8")
    restarted = JobManager(path)
    recovered = restarted.get(job_id)
    assert recovered["status"] == "interrupted"
    assert recovered["error"]["code"] == "service_restarted"


@pytest.fixture()
def api(tmp_path: Path):
    from agentloop_hub.api.server import JobManager, _make_handler

    _, hub, _ = make_project(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(hub, JobManager(), None))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], hub
    finally:
        httpd.shutdown()
        httpd.server_close()
        hub.close()


def _token(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/session", timeout=10) as res:
        return json.loads(res.read())["token"]


def _post(port: int, path: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-AgentMesh-Token": _token(port)},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_http_chain_draft_returns_409_on_stale_revision(api):
    port, _ = api
    body = {"goal": "g", "stages": [{"agent_id": "mock_worker",
                                     "model": "mock-model", "type": "analysis"}]}
    status, first = _post(port, "/api/chain/draft", {"draft": {**body, "base_revision": 0}})
    assert status == 200 and first["revision"] == 1
    status, second = _post(port, "/api/chain/draft", {"draft": {**body, "base_revision": 1}})
    assert status == 200 and second["revision"] == 2
    status, error = _post(port, "/api/chain/draft", {"draft": {**body, "base_revision": 1}})
    assert status == 409
    assert error["error"]["code"] == "conflict"
