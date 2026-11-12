"""WebUI close-out channels (2026-11-07 defects #9/#10/#11).

The real-chain test had to call adopt-attempt/submit-result/summary-submit over
raw APIs because the console offered no buttons; running nodes could not be
rebound without waiting for a timeout; the plan import relied on prompt(),
which ZCode's embedded browser does not implement.  These tests cover the new
HTTP surface and the static UI wiring.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from hub_test_support import default_plan, default_scenario, make_project


@pytest.fixture()
def api(tmp_path: Path):
    from agentloop_hub.api.server import JobManager, _make_handler

    repo, hub, _ = make_project(tmp_path)
    imported = hub.import_plan(default_plan(str(repo)), main_agent_id="mock_main",
                               model="mock-model")
    approved = hub.decide_approval(imported["approval_id"], approve=True)
    run_id = approved["outcome"]["run_id"]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(hub, JobManager(), None))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"port": httpd.server_address[1], "hub": hub, "run_id": run_id,
               "repo": repo}
    finally:
        httpd.shutdown()
        httpd.server_close()
        hub.close()


def _token(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/session", timeout=10) as res:
        return json.loads(res.read())["token"]


def _get(port: int, path: str) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"X-AgentMesh-Token": _token(port)},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _post(port: int, path: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-AgentMesh-Token": _token(port)},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_attempts_endpoint_lists_result_files(api):
    hub = api["hub"]
    task = hub.store.list_tasks(api["run_id"])[0]
    result_file = Path(api["repo"]) / ".agentloop" / "attempt-result.json"
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text('{"task_id": "T001", "summary": "ok"}', encoding="utf-8")
    hub.store.create_attempt(
        attempt_id="att_test_1", project_id=task.project_id, task_uid=task.uid,
        run_id=task.run_id, attempt_number=1, request_id="req_test_1",
        purpose="task", workdir=str(task.workdir or ""),
        result_path=str(result_file), binding={"marker": "M1"})
    status, body = _get(api["port"], f"/api/tasks/{task.uid}/attempts")
    assert status == 200
    assert body["attempts"][0]["attempt_id"] == "att_test_1"
    assert body["attempts"][0]["result_exists"] is True
    assert body["attempts"][0]["request_id"] == "req_test_1"


def test_summary_submit_accepts_a_project_file_path(api):
    port, repo = api["port"], api["repo"]
    project_file = repo / ".agentloop" / "summary-handin.json"
    project_file.parent.mkdir(parents=True, exist_ok=True)
    project_file.write_text(json.dumps({"summary_version": 1}), encoding="utf-8")
    status, body = _post(port, f"/api/runs/{api['run_id']}/summary",
                         {"path": str(project_file)})
    # The file was read and validated: missing required summary fields.
    assert status in (400, 409, 422), body
    assert "汇总" in json.dumps(body, ensure_ascii=False)
    outside = Path("C:/Windows/win.ini")
    status, body = _post(port, f"/api/runs/{api['run_id']}/summary",
                         {"path": str(outside)})
    assert status in (400, 403, 404), body
    assert "项目目录" in json.dumps(body, ensure_ascii=False)


def test_running_node_can_be_stopped_and_rebound(api):
    hub, port = api["hub"], api["port"]
    task = hub.store.list_tasks(api["run_id"])[0]
    hub.store.set_task_status(task.uid, "running", force=True)
    status, body = _post(port, f"/api/tasks/{task.uid}/binding", {
        "agent_id": "mock_verifier", "model": "mock-model", "stop_active": True,
    })
    assert status == 200, body
    assert body["assigned_agent"] == "mock_verifier"
    refreshed = hub.store.get_task(task.uid)
    assert refreshed.status == "blocked"
    assert "停止当前尝试并改绑" in (refreshed.error or "")
    # The audit trail records who stopped what.
    audit = [row for row in hub.store.list_audit(hub.project.project_id, limit=50)
             if row.get("action") == "task.stop_and_rebind"]
    assert audit
    # Without the explicit stop flag a running node is still protected.
    hub.store.set_task_status(task.uid, "running", force=True)
    status, body = _post(port, f"/api/tasks/{task.uid}/binding", {
        "agent_id": "mock_worker", "model": "mock-model",
    })
    assert status == 400, body


def test_console_has_no_native_prompt_and_ships_closeout_modals():
    static = Path(__file__).resolve().parents[1] / "agentloop_hub" / "api" / "static"
    app_js = (static / "app.js").read_text(encoding="utf-8")
    index_html = (static / "index.html").read_text(encoding="utf-8")
    assert "= prompt(" not in app_js
    for element in ("importPlanModal", "summaryModal", "adoptModal",
                    "textInputModal", "taskAttempts", "resultAttempt",
                    "bindingStopActive"):
        assert element in index_html, element
    for fn in ("openAdoptModal", "openSummaryModal", "openImportPlanModal",
               "submitSummaryJson", "loadTaskAttempts", "confirmAdopt"):
        assert fn in app_js, fn
    assert "pre.plain" in index_html
    # Stale manual-action cards are filtered by task/run state.
    assert "taskByUid" in app_js and "liveRun" in app_js

def test_late_outcome_after_stop_and_rebind_is_discarded(tmp_path):
    scenario = default_scenario()
    scenario["tasks"]["T001"]["sleep"] = 2.5
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    imported = hub.import_plan(default_plan(str(repo)), main_agent_id="mock_main",
                               model="mock-model")
    approved = hub.decide_approval(imported["approval_id"], approve=True)
    run_id = approved["outcome"]["run_id"]
    hub.start_run(run_id, max_parallel=1, background=True)
    deadline = time.time() + 10
    task = None
    while time.time() < deadline:
        task = next((t for t in hub.store.list_tasks(run_id)
                     if t.task_id == "T001"), None)
        if task and task.status == "running" and task.active_attempt_id:
            break
        time.sleep(0.05)
    assert task is not None and task.status == "running"
    old_attempt = task.active_attempt_id
    hub.update_task_binding(task.uid, agent_id="mock_verifier", model="mock-model",
                            stop_active=True)
    assert hub.store.get_task(task.uid).status == "blocked"
    deadline = time.time() + 12
    attempt = {}
    while time.time() < deadline:
        attempt = hub.store.get_attempt(old_attempt) or {}
        if attempt.get("status") == "canceled" and "晚到" in (attempt.get("error") or ""):
            break
        time.sleep(0.05)
    assert attempt.get("status") == "canceled", attempt
    refreshed = hub.store.get_task(task.uid)
    assert refreshed.status == "blocked", refreshed
    assert refreshed.assigned_agent == "mock_verifier"
    assert "停止当前尝试并改绑" in (refreshed.error or "")
    audit = [row for row in hub.store.list_audit(hub.project.project_id, limit=100)
             if row.get("action") == "task.stale_attempt_discarded"]
    assert audit
    hub.close()
