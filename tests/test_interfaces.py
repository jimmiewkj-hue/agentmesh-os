"""Unit tests for programmatic model/workspace/mode interfaces (no real GUI)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agentloop_hub.control import interfaces as iface


def _workbuddy_db(path: Path, *, cwd: str, model: str, source_mode: str = "craft",
                  permission_mode: str = "fullAccess", created_at: int = 1000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "create table if not exists sessions (id text primary key, cwd text, status text, "
        "mode text, model text, permission_mode text, source_mode text, created_at integer)"
    )
    conn.execute(
        "insert into sessions values (?,?,?,?,?,?,?,?)",
        ("sess-1", cwd, "completed", "craft", model, permission_mode, source_mode, created_at),
    )
    conn.commit()
    conn.close()
    return path


def _qoder_db(path: Path, *, cwd: str, model: str, session_id: str = "qoder-sess") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("create table workspaces (workspace_id text primary key, name text, "
                 "workspace_source text, root_paths_json text, extra_json text, "
                 "created_at integer, updated_at integer, archived integer, "
                 "deleted_at integer, product_mode text)")
    conn.execute("create table chat_sessions (session_id text primary key, cwd text, "
                 "workspace_id text, model text, title text, session_kind text, "
                 "deleted_at integer, updated_at integer, execution_target_json text)")
    conn.execute("create table chat_model_preferences (model_key text primary key, "
                 "visible integer, context_window integer, reasoning_effort text, "
                 "updated_at integer)")
    conn.execute("insert into workspaces values (?,?,?,?,?,?,?,?,?,?)",
                 ("ws-old", "old", "local", json.dumps([cwd]), "{}", 1, 1, 0, None, "coding"))
    conn.execute("insert into chat_sessions values (?,?,?,?,?,?,?,?,?)",
                 (session_id, cwd, "ws-old", model, "probe", "standard", None, 1,
                  json.dumps({"workspace": {"id": "ws-old", "path": cwd}})))
    for key in ("dfmodel", "gfmodel"):
        conn.execute("insert into chat_model_preferences values (?,?,?,?,?)",
                     (key, 1, None, None, 1))
    conn.commit()
    conn.close()
    return path


def _qoder_transcript(projects: Path, cwd: str, session_id: str, *,
                      model: str = "dfmodel", workspace_event: str = "") -> Path:
    folder = projects / iface.qoder_project_slug(cwd)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session_id}.jsonl"
    events = [
        {"type": "workspace-directories", "sessionId": session_id,
         "directories": [workspace_event or cwd]},
        {"type": "runtime-config", "sessionId": session_id, "model": model},
        {"type": "assistant", "sessionId": session_id,
         "message": {"role": "assistant", "model": model, "stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "OK"}]}},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def test_workbuddy_deeplink_encodes_all_programmatic_fields(tmp_path):
    captured: list[str] = []
    outcome = iface.prepare_workbuddy_task(
        workspace=str(tmp_path / "ws"), model="GLM-5.3-Flash", prompt="hello",
        welcome_mode="code", permission_mode="fullAccess", launch=captured.append,
        config={"session_db": str(tmp_path / "wb.db")},
    )
    assert outcome.ok
    assert captured
    uri = captured[0]
    assert uri.startswith("workbuddy://task?")
    assert "model=glm-5.3-flash" in uri
    assert "welcomeMode=code" in uri
    assert "permissionMode=fullAccess" in uri
    assert f"cwd={str(tmp_path / 'ws').replace(chr(92), '%5C')}" in uri or "cwd=" in uri


def test_workbuddy_model_not_in_catalog_is_reported(tmp_path):
    outcome = iface.prepare_workbuddy_task(
        workspace=str(tmp_path / "ws"), model="not-a-real-model", prompt="x",
        launch=lambda _uri: None,
        config={"session_db": str(tmp_path / "wb.db")},
    )
    assert not outcome.ok
    assert outcome.error_code == "MODEL_UNAVAILABLE"


def test_workbuddy_workspace_and_mode_verification(tmp_path):
    workspace = str(tmp_path / "ws")
    db = _workbuddy_db(tmp_path / "wb.db", cwd=workspace, model="glm-5.3-flash")
    ok = iface.verify_workbuddy_task(workspace=workspace, model="GLM-5.3-Flash",
                                     started_at=0, config={"session_db": str(db)})
    assert ok.ok
    assert ok.actual["source_mode"] == "craft"

    db2 = _workbuddy_db(tmp_path / "wb2.db", cwd=str(tmp_path / "other"),
                        model="glm-5.3-flash")
    bad = iface.verify_workbuddy_task(workspace=workspace, model="GLM-5.3-Flash",
                                      started_at=0, config={"session_db": str(db2)})
    assert bad.error_code == "WORKSPACE_MISMATCH"

    db3 = _workbuddy_db(tmp_path / "wb3.db", cwd=workspace, model="glm-5.3-flash",
                        source_mode="working")
    mode = iface.verify_workbuddy_task(workspace=workspace, model="GLM-5.3-Flash",
                                       started_at=0, config={"session_db": str(db3)})
    assert mode.error_code == "GUI_MODE_MISMATCH"


def test_qoder_project_slug_and_transcript_rewrite(tmp_path):
    assert iface.qoder_project_slug(r"D:\qoder\agent-hub") == "J--qoder-agent-hub"
    assert iface.qoder_project_slug(
        r"D:\a\.agentloop\planning\plan_x\workspace"
    ) == "J--a--agentloop-planning-plan-x-workspace"
    path = _qoder_transcript(tmp_path / "projects", r"D:\old", "s1")
    changed = iface._rewrite_qoder_transcript(
        path, workspace=r"D:\new", model_key="gfmodel",
    )
    assert changed >= 2
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert events[0]["directories"] == [r"D:\new"]
    assert events[-1]["type"] == "runtime-config"
    assert events[-1]["model"] == "gfmodel"


def test_qoder_verification_detects_workspace_and_model_mismatch(tmp_path):
    workspace = r"D:\run\ws"
    db = _qoder_db(tmp_path / "main.sqlite", cwd=workspace, model="dfmodel")
    _qoder_transcript(tmp_path / "projects", workspace, "qoder-sess", model="dfmodel")
    ok = iface.verify_qoder_session(
        workspace=workspace, model="DeepSeek-Flash", started_at=0,
        config={"qoder_db": str(db), "qoder_projects": str(tmp_path / "projects"),
                "qoder_daemon_log": str(tmp_path / "no-daemon.log")},
    )
    assert ok.ok

    _qoder_transcript(tmp_path / "projects2", r"D:\other", "qoder-sess",
                      model="dfmodel", workspace_event=r"D:\other")
    bad = iface.verify_qoder_session(
        workspace=r"D:\run\ws2", model="DeepSeek-Flash", started_at=0,
        config={"qoder_db": str(db), "qoder_projects": str(tmp_path / "projects2"),
                "qoder_daemon_log": str(tmp_path / "no-daemon.log")},
    )
    assert bad.error_code == "WORKSPACE_MISMATCH"

    _qoder_transcript(tmp_path / "projects3", workspace, "qoder-sess", model="dfmodel")
    wrong_model = iface.verify_qoder_session(
        workspace=workspace, model="GLM-5.3-Flash", started_at=0,
        config={"qoder_db": str(db), "qoder_projects": str(tmp_path / "projects3"),
                "qoder_daemon_log": str(tmp_path / "no-daemon.log")},
    )
    assert wrong_model.error_code == "GUI_MODEL_SWITCH_FAILED"


def test_qoder_prepare_session_moves_transcript_and_updates_db(tmp_path, monkeypatch):
    workspace = str(tmp_path / "run-ws")
    db = _qoder_db(tmp_path / "main.sqlite", cwd=str(tmp_path / "old-ws"), model="gfmodel")
    projects = tmp_path / "projects"
    _qoder_transcript(projects, str(tmp_path / "old-ws"), "qoder-sess", model="gfmodel")

    monkeypatch.setattr(iface, "_stop_process", lambda _name: None)
    monkeypatch.setattr(iface, "_start_app", lambda _exe: None)
    outcome = iface.prepare_qoder_session(
        workspace=workspace, model="DeepSeek-Flash",
        config={"qoder_db": str(db), "qoder_projects": str(projects)},
        allow_restart=True, allow_history_migration=True,
    )
    assert outcome.ok, outcome
    conn = sqlite3.connect(db)
    row = conn.execute("select cwd, model from chat_sessions where session_id='qoder-sess'").fetchone()
    conn.close()
    assert row == (workspace, "dfmodel")
    new_path = projects / iface.qoder_project_slug(workspace) / "qoder-sess.jsonl"
    assert new_path.exists()
    # The next turn answers with the model written by prepare; the old
    # assistant events still carry the previous model, so append the run's
    # answering event before verifying.
    with new_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "assistant", "sessionId": "qoder-sess",
            "message": {"role": "assistant", "model": "dfmodel",
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "OK"}]},
        }) + "\n")
    verify = iface.verify_qoder_session(
        workspace=workspace, model="DeepSeek-Flash", started_at=0,
        config={"qoder_db": str(db), "qoder_projects": str(projects),
                "qoder_daemon_log": str(tmp_path / "no-daemon.log")},
        session_id="qoder-sess",
    )
    assert verify.ok, verify


def test_qoder_prepare_falls_back_to_session_with_transcript(tmp_path, monkeypatch):
    workspace = str(tmp_path / "run-ws")
    old_ws = str(tmp_path / "old-ws")
    db = _qoder_db(tmp_path / "main.sqlite", cwd=old_ws, model="dfmodel")
    projects = tmp_path / "projects"
    _qoder_transcript(projects, old_ws, "qoder-sess", model="dfmodel")
    conn = sqlite3.connect(db)
    # Newer empty chat created by the GUI "+" before the adapter runs.
    conn.execute("insert into chat_sessions values (?,?,?,?,?,?,?,?,?)",
                 ("empty-sess", str(tmp_path / "other-ws"), "ws-old", "dfmodel",
                  "empty", "standard", None, 999999, "{}"))
    conn.commit()
    conn.close()
    assert iface._qoder_current_session(Path(db))["session_id"] == "empty-sess"

    monkeypatch.setattr(iface, "_stop_process", lambda _name: None)
    monkeypatch.setattr(iface, "_start_app", lambda _exe: None)
    outcome = iface.prepare_qoder_session(
        workspace=workspace, model="DeepSeek-Flash",
        config={"qoder_db": str(db), "qoder_projects": str(projects)},
        allow_restart=True, allow_history_migration=True,
    )
    assert outcome.ok, outcome
    assert outcome.evidence["session_id"] == "qoder-sess"
    assert outcome.evidence["fallback_session_id"] == "qoder-sess"


def test_qoder_daemon_cwd_mismatch_is_detected(tmp_path):
    workspace = r"D:\run\daemon-ws"
    db = _qoder_db(tmp_path / "main.sqlite", cwd=workspace, model="dfmodel")
    _qoder_transcript(tmp_path / "projects", workspace, "qoder-sess", model="dfmodel")
    daemon_log = tmp_path / "qoder-agent-sdk.log"
    daemon_log.write_text(
        "[DaemonSession] sessionId=x session opened cwd=C:\\Users\\<user> workerArgs=47\n",
        encoding="utf-8",
    )
    parsed = iface.qoder_daemon_cwd({"qoder_daemon_log": str(daemon_log)})
    assert parsed["cwd"] == r"C:\Users\<user>"
    outcome = iface.verify_qoder_session(
        workspace=workspace, model="DeepSeek-Flash", started_at=0,
        config={"qoder_db": str(db), "qoder_projects": str(tmp_path / "projects"),
                "qoder_daemon_log": str(daemon_log)},
        session_id="qoder-sess",
    )
    assert outcome.error_code == "WORKSPACE_MISMATCH"
    assert "daemon open_session cwd" in outcome.reason


def test_qoder_daemon_cwd_lookup_is_per_session(tmp_path):
    workspace = r"D:\agentmesh-e2e\q1-20261103"
    db = _qoder_db(tmp_path / "main.sqlite", cwd=workspace, model="dfmodel",
                   session_id="44264eef")
    _qoder_transcript(tmp_path / "projects", workspace, "44264eef")
    daemon_log = tmp_path / "qoder-agent-sdk.log"
    daemon_log.write_text(
        "[DaemonSession] sessionId=44264eef session opened cwd=D:\\agentmesh-e2e\\q1-20261103 workerArgs=69\n"
        "[DaemonSession] sessionId=bb70ac03 session opened cwd=C:\\Users\\<user> workerArgs=49\n",
        encoding="utf-8",
    )
    parsed = iface.qoder_daemon_cwd({"qoder_daemon_log": str(daemon_log)},
                                    session_id="44264eef")
    assert parsed["session_cwd"] == workspace
    assert parsed["last_cwd"] == r"C:\Users\<user>"
    outcome = iface.verify_qoder_session(
        workspace=workspace, model="DeepSeek-Flash", started_at=0,
        config={"qoder_db": str(db), "qoder_projects": str(tmp_path / "projects"),
                "qoder_daemon_log": str(daemon_log)},
        session_id="44264eef",
    )
    assert outcome.ok, outcome
    assert outcome.actual["daemon_session_cwd"] == workspace


def test_qoder_verify_uses_answering_model_over_trailing_runtime_config(tmp_path):
    workspace = r"D:\run\model-ws"
    db = _qoder_db(tmp_path / "main.sqlite", cwd=workspace, model="dfmodel")
    folder = tmp_path / "projects" / iface.qoder_project_slug(workspace)
    folder.mkdir(parents=True)
    path = folder / "qoder-sess.jsonl"
    events = [
        {"type": "workspace-directories", "sessionId": "qoder-sess",
         "directories": [workspace]},
        {"type": "assistant", "sessionId": "qoder-sess",
         "message": {"role": "assistant", "model": "dfmodel", "stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "OK"}]}},
        # The desktop writes the next turn's config after the reply; it must
        # not override the model that actually answered.
        {"type": "runtime-config", "sessionId": "qoder-sess", "model": "auto"},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    outcome = iface.verify_qoder_session(
        workspace=workspace, model="DeepSeek-Flash", started_at=0,
        config={"qoder_db": str(db), "qoder_projects": str(tmp_path / "projects"),
                "qoder_daemon_log": str(tmp_path / "no-daemon.log")},
        session_id="qoder-sess",
    )
    assert outcome.ok, outcome
    assert outcome.actual["transcript_model"] == "dfmodel"
    assert outcome.actual["runtime_config_model"] == "auto"


def test_set_model_programmatically_writes_qoder_default(tmp_path):
    default = tmp_path / "models-default.json"
    db = _qoder_db(tmp_path / "main.sqlite", cwd=r"D:\ws", model="dfmodel")
    outcome = iface.set_model_programmatically(
        "qoder_cn", "GLM-5.3-Flash",
        config={"qoder_db": str(db), "qoder_models_default": str(default)},
    )
    assert outcome.ok
    payload = json.loads(default.read_text(encoding="utf-8"))
    assert payload["key"] == "gfmodel"
