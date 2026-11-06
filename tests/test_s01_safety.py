"""S01 safety contracts: no synthetic success, no unowned side effects.

Each test maps to an audit finding (F01/F03/F04/F05/F13) and must fail if the
old permissive behaviour returns.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from agentloop_hub.config import HubConfig
from agentloop_hub.control import interfaces as iface
from agentloop_hub.control import trae_interface
from agentloop_hub.gui import permissions as permissions_module
from agentloop_hub.gui.base import GuiAutomationError, GuiProfile
from agentloop_hub.gui.readers import WorkBuddyReader
from agentloop_hub.gui.trae_reader import TraeReader


# ---------------------------------------------------------------------------
# F05: strict scope policy survives a config round-trip; invalid values fail.
# ---------------------------------------------------------------------------

def test_scope_policy_round_trip_and_validation():
    cfg = HubConfig(scope_violation_policy="strict")
    restored = HubConfig.from_dict(cfg.to_dict())
    assert restored.scope_violation_policy == "strict"

    merged = HubConfig().merged({"scope_violation_policy": "strict"})
    assert merged.scope_violation_policy == "strict"

    with pytest.raises(ValueError):
        HubConfig.from_dict({"scope_violation_policy": "lenient"})


# ---------------------------------------------------------------------------
# F01: file / unknown status cannot complete a WorkBuddy turn.
# ---------------------------------------------------------------------------

def _profile(root: Path) -> GuiProfile:
    return GuiProfile(
        app="workbuddy", product="WorkBuddy", window_title="WorkBuddy",
        executable_candidates=(), transcript_reader="workbuddy",
        data_root=str(root),
    )


def _workbuddy_fixture(root: Path, status: str) -> tuple[Path, Path]:
    project = root / "demo"
    project.mkdir(parents=True)
    transcript = project / "sess-1.jsonl"
    events = [
        {"type": "message", "role": "user", "sessionId": "sess-1",
         "content": "task MARKER_UNKNOWN"},
        {"type": "message", "role": "assistant", "status": "completed",
         "sessionId": "sess-1",
         "providerData": {"model": "glm-5.3-flash"},
         "content": [{"type": "output_text", "text": "intermediate answer"}]},
    ]
    transcript.write_text("\n".join(json.dumps(e) for e in events) + "\n",
                          encoding="utf-8")
    db = root / "workbuddy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions(id TEXT PRIMARY KEY, cwd TEXT, title TEXT, status TEXT,"
        " model TEXT, created_at INTEGER, updated_at INTEGER, last_activity_at INTEGER)")
    now_ms = int(time.time() * 1000)
    conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)",
                 ("sess-1", str(root), "demo", status, "glm-5.3-flash",
                  now_ms, now_ms, now_ms))
    conn.commit()
    conn.close()
    result = root / "result.json"
    result.write_text('{"task_id":"T1"}', encoding="utf-8")
    return transcript, result


@pytest.mark.parametrize("status", ["working", "unknown", ""])
def test_workbuddy_result_file_never_completes_without_explicit_status(
        tmp_path: Path, status: str):
    _, result = _workbuddy_fixture(tmp_path, status)
    reader = WorkBuddyReader(
        _profile(tmp_path),
        {"session_db": str(tmp_path / "workbuddy.db"),
         "workbuddy_result_settle_seconds": 0.1, "poll_seconds": 0.05,
         "quiet_seconds": 0.05, "send_timeout_seconds": 5},
    )
    reader.completion_path = str(result)
    with pytest.raises(GuiAutomationError) as info:
        reader.wait_reply("MARKER_UNKNOWN", started_at=time.time() - 2,
                          timeout=0.6, stall_seconds=60)
    assert info.value.code == "GUI_TIMEOUT"


def test_workbuddy_completed_status_still_finishes(tmp_path: Path):
    _, result = _workbuddy_fixture(tmp_path, "completed")
    reader = WorkBuddyReader(
        _profile(tmp_path),
        {"session_db": str(tmp_path / "workbuddy.db"),
         "workbuddy_result_settle_seconds": 0.1, "poll_seconds": 0.05,
         "quiet_seconds": 0.05, "send_timeout_seconds": 5},
    )
    reader.completion_path = str(result)
    reply = reader.wait_reply("MARKER_UNKNOWN", started_at=time.time() - 2,
                              timeout=5, stall_seconds=60)
    assert reply.session_id == "sess-1"


# ---------------------------------------------------------------------------
# F01/F03: Trae result file is evidence, not a terminal signal.
# ---------------------------------------------------------------------------

def _trae_state(tmp_path: Path, *, finished: bool, messages: int):
    session = {"sessionId": "trae-sess", "messageIds": [str(i) for i in range(messages)],
               "assistant_task_status": "finished" if finished else "working"}
    values = {"solo-lite-mode-state-map-uid": json.dumps({"code": {"session": session}})}
    return trae_interface.TraeState(app="trae_cn", uid="uid",
                                    db=tmp_path / "state.vscdb", items=values)


def test_trae_result_file_does_not_finish_while_task_still_working(tmp_path: Path):
    result = tmp_path / "plan.json"
    result.write_text('{"goal":"x","tasks":[]}', encoding="utf-8")
    state = {"value": _trae_state(tmp_path, finished=False, messages=2)}
    text = {"value": {"text": "MARKER_TRAE reply", "page_id": "page-1"}}
    reader = TraeReader(
        GuiProfile(app="trae_cn", product="Trae CN", window_title="Trae",
                   executable_candidates=(), transcript_reader="trae"),
        {"trae_poll_seconds": 0.05, "trae_stable_seconds": 0.05},
        state_reader=lambda: state["value"],
        text_reader=lambda: text["value"],
    )
    reader.completion_path = str(result)
    with pytest.raises(GuiAutomationError) as info:
        reader.wait_reply("MARKER_TRAE", started_at=time.time() - 2,
                          timeout=0.5, stall_seconds=0)
    assert info.value.code == "GUI_TIMEOUT"

    state["value"] = _trae_state(tmp_path, finished=False, messages=2)

    def finish_turn() -> None:
        time.sleep(0.2)
        state["value"] = _trae_state(tmp_path, finished=True, messages=3)

    thread = threading.Thread(target=finish_turn)
    thread.start()
    reply = reader.wait_reply("MARKER_TRAE", started_at=time.time() - 2,
                              timeout=5, stall_seconds=0)
    thread.join(timeout=2)
    assert reply.session_id == "trae-sess"
    assert "reply" in reply.text


def test_trae_cdp_workspace_open_failure_is_propagated(monkeypatch):
    def fail_open(**_kwargs):
        return iface.InterfaceOutcome(False, "cli", {"workspace": "W"}, {},
                                      reason="folder open rejected",
                                      error_code="WORKSPACE_SWITCH_FAILED")

    monkeypatch.setattr(trae_interface, "_cdp_alive", lambda _port: True)
    monkeypatch.setattr(trae_interface, "open_trae_workspace", fail_open)
    outcome = trae_interface.ensure_trae_cdp(app="trae_cn", port=9333,
                                             workspace="W")
    assert outcome.ok is False
    assert outcome.error_code == "WORKSPACE_SWITCH_FAILED"
    assert "folder open rejected" in outcome.reason


# ---------------------------------------------------------------------------
# F04: full_control off must not request fullAccess, and a broader session
# is blocked on readback.
# ---------------------------------------------------------------------------

def test_workbuddy_deeplink_omits_permission_mode_when_not_granted():
    link = iface.build_workbuddy_deeplink(
        workspace="W", model_key="m", prompt="p", permission_mode="")
    assert "permissionMode" not in link
    explicit = iface.build_workbuddy_deeplink(
        workspace="W", model_key="m", prompt="p", permission_mode="fullAccess")
    assert "permissionMode=fullAccess" in explicit


def test_workbuddy_permission_readback_blocks_full_access_without_control(tmp_path):
    db = tmp_path / "workbuddy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions(id TEXT PRIMARY KEY, cwd TEXT, status TEXT, mode TEXT,"
        " model TEXT, permission_mode TEXT, source_mode TEXT, created_at INTEGER)")
    now_ms = int(time.time() * 1000)
    conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)",
                 ("sess-1", "W", "completed", "craft", "glm-5.3-flash",
                  "fullAccess", "craft", now_ms))
    conn.commit()
    conn.close()
    outcome = iface.verify_workbuddy_task(
        workspace="W", model="GLM-5.3-Flash", started_at=0,
        session_id="sess-1", permission_mode="",
        config={"session_db": str(db)},
    )
    assert outcome.ok is False
    assert outcome.error_code == "PERMISSION_TOO_BROAD"


def test_permission_watcher_uses_the_bound_page(monkeypatch):
    seen: list[str] = []

    def fake_accept(app: str, port: int, page_id: str = "") -> dict:
        seen.append(page_id)
        return {"ok": False, "reason": "none"}

    monkeypatch.setattr(permissions_module, "accept_permission_once", fake_accept)
    watcher = permissions_module.PermissionWatcher(
        app="workbuddy", port=9223, page_id="page-42", interval=0.01,
    ).start()
    time.sleep(0.05)
    watcher.stop()
    assert seen and set(seen) == {"page-42"}


# ---------------------------------------------------------------------------
# F03: normal Qoder preparation cannot stop/migrate the user's session.
# ---------------------------------------------------------------------------

def test_qoder_prepare_blocks_without_explicit_restart_and_migration(
        tmp_path: Path, monkeypatch):
    calls: list[str] = []

    def fake_stop(name: str) -> None:
        calls.append(name)

    monkeypatch.setattr(iface, "_stop_process", fake_stop)
    outcome = iface.prepare_qoder_session(
        workspace=str(tmp_path / "ws"), model="DeepSeek-Flash",
        config={"qoder_db": str(tmp_path / "main.sqlite"),
                "qoder_projects": str(tmp_path / "projects")},
    )
    assert outcome.ok is False
    assert outcome.error_code == "SESSION_BINDING_UNCONFIRMED"
    assert calls == []


# ---------------------------------------------------------------------------
# F13: local HTTP control surface rejects cross-origin / untyped / untokened
# writes, and never leaks a traceback.
# ---------------------------------------------------------------------------

@pytest.fixture()
def api_server(tmp_path: Path):
    from agentloop_hub.api.server import JobManager, _make_handler
    from hub_test_support import make_project

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


def _raw_post(port: int, path: str, body: dict, headers: dict[str, str]) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"), method="POST", headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _token(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/session", timeout=10) as res:
        return json.loads(res.read())["token"]


def test_http_write_requires_token_json_and_local_origin(api_server):
    port, hub = api_server
    body = {"goal": "x", "tasks": []}
    status, payload = _raw_post(
        port, "/api/ask", body,
        {"Content-Type": "application/json", "Origin": "https://evil.example"})
    assert status == 403
    assert "跨源" in payload["error"]["message"]

    status, _ = _raw_post(
        port, "/api/ask", body,
        {"Content-Type": "text/plain", "X-AgentMesh-Token": _token(port)})
    assert status == 403

    status, _ = _raw_post(
        port, "/api/ask", body, {"Content-Type": "application/json"})
    assert status == 403

    status, created = _raw_post(
        port, "/api/ask", body,
        {"Content-Type": "application/json", "X-AgentMesh-Token": _token(port)})
    assert status == 200
    assert created["plan_id"]


def test_http_internal_error_has_no_traceback(api_server):
    from agentloop_hub.api import server as server_module

    port, hub = api_server

    def boom(*_args, **_kwargs):
        raise RuntimeError("secret path C:/private")

    original = hub.ask
    hub.ask = boom  # type: ignore[method-assign]
    try:
        status, payload = _raw_post(
            port, "/api/ask", {"goal": "x"},
            {"Content-Type": "application/json", "X-AgentMesh-Token": _token(port)})
    finally:
        hub.ask = original  # type: ignore[method-assign]
    assert status == 500
    assert "traceback" not in payload["error"]
    assert "secret path" not in json.dumps(payload, ensure_ascii=False)
    assert payload["error"]["error_id"]


def test_workbuddy_db_and_response_model_conflict_is_an_error(tmp_path: Path):
    _, result = _workbuddy_fixture(tmp_path, "completed")
    conn = sqlite3.connect(tmp_path / "workbuddy.db")
    conn.execute("UPDATE sessions SET model='other-model' WHERE id='sess-1'")
    conn.commit()
    conn.close()
    reader = WorkBuddyReader(
        _profile(tmp_path),
        {"session_db": str(tmp_path / "workbuddy.db"),
         "workbuddy_result_settle_seconds": 0.1, "poll_seconds": 0.05,
         "quiet_seconds": 0.05, "send_timeout_seconds": 5},
    )
    reader.completion_path = str(result)
    with pytest.raises(GuiAutomationError) as info:
        reader.wait_reply("MARKER_UNKNOWN", started_at=time.time() - 2,
                          timeout=2, stall_seconds=60)
    assert info.value.code == "GUI_MODEL_SWITCH_FAILED"
    assert "不一致" in info.value.message


def test_process_identity_records_pid_and_start_time():
    import os

    from agentloop_hub.gui.windows import process_identity, window_process_identity

    identity = process_identity(os.getpid())
    if os.name == "nt":
        assert identity.get("pid") == os.getpid()
        assert float(identity.get("started_epoch") or 0) > 0
        assert identity.get("started_at")
    else:
        assert identity == {}
    assert window_process_identity(0) == {}
