from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import http.client
from urllib.parse import urlparse

from agentloop_hub.api.server import JobManager, _make_handler
from agentloop_hub.workspace import WorkspaceManager
from hub_test_support import default_plan, make_project


@pytest.fixture()
def api(tmp_path):
    repo, hub, _ = make_project(tmp_path)
    jobs = JobManager()
    handler = _make_handler(hub, jobs)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, hub, jobs
    server.shutdown()
    server.server_close()
    hub.close()


def get(base: str, path: str) -> tuple[int, bytes]:
    req = urllib.request.Request(base + path)
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def control_token(base: str) -> str:
    with urllib.request.urlopen(base + "/api/session", timeout=10) as res:
        return json.loads(res.read())["token"]


def post(base: str, path: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "X-AgentMesh-Token": control_token(base)})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_static_and_core_endpoints(api):
    base, hub, _ = api
    status, body = get(base, "/")
    assert status == 200 and b"AgentMesh OS" in body
    status, body = get(base, "/api/health")
    assert status == 200 and json.loads(body)["status"] == "ok"
    for path in ("/api/overview", "/api/agents", "/api/tasks", "/api/approvals",
                 "/api/costs", "/api/audit", "/api/memory", "/api/conversations",
                 "/api/connectors", "/api/surface", "/api/bus", "/api/bus/stats", "/api/bus/verify",
                 "/api/bus/subscriptions", "/api/router/rank?task_type=analysis",
                 "/api/router/scores", "/api/router/decisions", "/api/benchmark",
                 "/api/cost/report", "/api/sync", "/api/memory/bootstrap",
                 "/api/tasks/run_none:T1/logs"):
        status, _ = get(base, path)
        assert status in (200, 404), (path, status)


def test_surface_and_zero_input_health(api):
    base, hub, _ = api
    status, body = get(base, "/api/surface")
    assert status == 200
    data = json.loads(body)
    assert data["zero_input"]["mode"] == "blocked"
    assert {a["agent_id"] for a in data["agents"]} == {"mock_main", "mock_worker", "mock_verifier"}
    status, body = get(base, "/api/surface/mock_main")
    assert status == 200 and json.loads(body)["agent_id"] == "mock_main"
    status, body = get(base, "/api/surface/nope")
    assert status == 404
    status, body = get(base, "/api/health")
    assert json.loads(body)["zero_input"]["os_input"] == "blocked"


def test_plan_import_approve_and_run_via_api(api):
    base, hub, _ = api
    status, data = post(base, "/api/plans/import", {"plan": default_plan(base)})
    assert status == 200, data
    plan_id = data["plan_id"]
    approval_id = data["approval_id"]
    status, approved = post(base, f"/api/plans/{plan_id}/approve",
                            {"approve": True, "approval_id": approval_id})
    assert status == 200 and approved["status"] == "approved"
    run_id = approved["run_id"]
    status, started = post(base, f"/api/runs/{run_id}/start", {"background": True, "max_parallel": 2})
    assert status == 202
    deadline = time.time() + 45
    final = None
    while time.time() < deadline:
        status, run = get(base, f"/api/runs/{run_id}")
        final = json.loads(run)
        if final["run"]["status"] in ("pending_acceptance", "pending_merge", "failed", "waiting_user"):
            break
        time.sleep(1)
    assert final and final["run"]["status"] == "pending_acceptance", final
    approvals = json.loads(get(base, "/api/approvals")[1])["approvals"]
    acceptance = [a for a in approvals if a["kind"] == "acceptance" and a["status"] == "pending"]
    assert acceptance
    status, accepted = post(base, f"/api/approvals/{acceptance[0]['approval_id']}", {"approve": True})
    assert status == 200
    assert accepted["outcome"]["status"] == "pending_merge", accepted
    approvals = json.loads(get(base, "/api/approvals")[1])["approvals"]
    merge = [a for a in approvals if a["kind"] == "merge" and a["status"] == "pending"]
    assert merge
    status, decided = post(base, f"/api/approvals/{merge[0]['approval_id']}", {"approve": True})
    assert status == 200
    assert decided["outcome"]["status"] == "completed", decided


def test_agentmesh_bus_router_cost_sync_endpoints(api):
    base, hub, _ = api
    status, body = get(base, "/api/connectors")
    assert status == 200
    assert json.loads(body)["protocol"] == "agentmesh.connector.v1"
    status, published = post(base, "/api/bus/publish",
                             {"kind": "prompt", "source": "worker", "payload": {"x": 1}})
    assert status == 200 and published["seq"] >= 1
    status, sub = post(base, "/api/bus/subscribe",
                       {"subscriber": "worker", "kinds": ["prompt"]})
    assert status == 200
    status, polled = post(base, "/api/bus/poll",
                          {"subscription_id": sub["subscription_id"]})
    assert status == 200 and polled["count"] >= 1
    status, data = post(base, "/api/sync", {})
    assert status == 200 and data["status"] == "synced"
    status, data = post(base, "/api/benchmark", {})
    assert status == 200 and data["agents"]
    status, data = get(base, "/api/memory/bootstrap?agent_id=mock_worker")
    assert status == 200
    assert json.loads(data)["groups"]["project_index"]
    status, data = post(base, "/api/cache/clear", {})
    assert status == 200 and data["status"] == "cleared"
    status, data = get(base, "/api/router/rank?task_type=implementation")
    assert status == 200
    assert json.loads(data)["selected"]["agent_id"] != "mock_main"


def test_main_and_agent_endpoints(api):
    base, hub, _ = api
    status, body = get(base, "/api/main")
    assert status == 200 and json.loads(body)["main"]["agent_id"] == "mock_main"
    status, data = post(base, "/api/main", {"agent_id": "mock_verifier", "model": "m2"})
    assert status == 200 and data["main"]["agent_id"] == "mock_verifier"
    status, data = post(base, "/api/agents/discover", {})
    assert status == 200 and "agents" in data
    status, data = get(base, "/api/agents/mock_worker/health")
    assert status == 200
    assert "metrics" in json.loads(data)


def test_auto_main_and_budget_endpoints(api):
    base, hub, _ = api
    status, data = post(base, "/api/main/auto", {})
    assert status == 200, data
    assert data["agent_id"] != "mock_main"
    assert data["main"]["agent_id"] == data["agent_id"]
    assert data["selection"]
    status, body = get(base, "/api/budget")
    assert status == 200
    status, data = post(base, "/api/budget", {"tokens": 500000})
    assert status == 200 and data["token_budget"] == 500000
    status, body = get(base, "/api/budget")
    assert json.loads(body)["project_budget"] == 500000


def test_api_errors(api):
    base, _, _ = api
    status, body = get(base, "/api/does-not-exist")
    assert status == 404
    status, body = post(base, "/api/plans/does-not-exist/approve", {"approve": True})
    assert status in (400, 404, 409)
    status, body = post(base, "/api/plans/import", {"plan": {"goal": ""}})
    assert status == 422 or status == 400


def test_models_versions_and_mcp_endpoints(api):
    base, hub, _ = api
    status, body = get(base, "/api/agents/models")
    data = json.loads(body)
    assert status == 200
    assert data["by_agent"]["mock_worker"]["models"]
    status, body = post(base, "/api/agents/mock_worker/model", {"model": "mock-pro"})
    assert status == 200 and body["selected_model"] == "mock-pro"
    status, body = get(base, "/api/agents/version-status")
    assert status == 200 and json.loads(body)["agents"]
    status, body = get(base, "/api/agents/versions?agent_id=mock_worker")
    assert status == 200 and json.loads(body)["versions"]
    status, body = get(base, "/api/workspace")
    assert status == 200 and json.loads(body)["open"] is True
    status, body = get(base, "/api/agents/mock_worker/mcp/tools")
    assert status in (400, 500)


def test_workspace_init_and_switch_via_api(tmp_path):
    manager = WorkspaceManager(None, auto_discover=False)
    jobs = JobManager()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(None, jobs, manager))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, body = get(base, "/api/workspace")
        assert status == 200 and json.loads(body)["open"] is False
        target = tmp_path / "web-project"
        status, data = post(base, "/api/workspace/init",
                            {"path": str(target), "name": "Web", "create": True,
                             "git_init": True})
        assert status == 200, data
        assert data["workspace"]["open"] is True
        assert (target / ".git").exists()
        status, body = get(base, "/api/overview")
        assert status == 200
        assert json.loads(body)["project"]["root"] == str(target.resolve())
        status, data = post(base, "/api/workspace/close", {})
        assert status == 200 and data["workspace"]["open"] is False
    finally:
        server.shutdown()
        server.server_close()
        manager.close()


def test_sse_stream_delivers_bus_events(api):
    base, hub, _ = api
    parsed = urlparse(base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    try:
        conn.request("GET", "/api/stream?interval=0.5")
        resp = conn.getresponse()
        assert resp.status == 200
        assert "text/event-stream" in resp.getheader("Content-Type", "")
        first = []
        for _ in range(20):
            line = resp.readline().decode("utf-8", errors="replace").strip()
            if not line:
                continue
            first.append(line)
            if line.startswith("event: hello"):
                break
        assert any(line.startswith("event: hello") for line in first), first

        hub.bus_publish("prompt", {"stream": "ok"}, source="test")
        seen = False
        for _ in range(80):
            line = resp.readline().decode("utf-8", errors="replace").strip()
            if line.startswith("event: bus"):
                seen = True
                break
        assert seen
    finally:
        conn.close()
