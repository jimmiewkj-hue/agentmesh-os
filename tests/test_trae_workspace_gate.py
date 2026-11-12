"""Trae reuse-path workspace gate: a opened folder window is proof enough.

2026-11-11 real test: ``--new-window --folder-uri`` opened the plan worktree
and its composer mounted, but the global solo-lite project cache was never
written by a plain folder window.  The old gate waited only for that cache
and aborted with WORKSPACE_MISMATCH, leaving the prompt unsent.
"""
from __future__ import annotations

import pytest

from agentloop_hub.control import apps
from agentloop_hub.control import trae_interface as ti
from agentloop_hub.gui import cdp as cdp_mod


class FakeState:
    def __init__(self, tmp_path):
        self.db = tmp_path / "state.vscdb"

    def get(self, key, default=None):
        return default


@pytest.fixture()
def gate_env(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    exe = tmp_path / "Trae CN.exe"
    exe.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(ti, "current_trae_workspace", lambda *a, **k: "j:\\other")
    monkeypatch.setattr(ti, "trae_executable", lambda *a, **k: exe)
    monkeypatch.setattr(apps, "launch_detached", lambda *a, **k: 123)
    monkeypatch.setattr(ti, "_cdp_alive", lambda port: True)
    monkeypatch.setattr(ti, "dismiss_trae_trust_dialog",
                        lambda *a, **k: {"ok": True, "dismissed": False})
    monkeypatch.setattr(ti, "read_state", lambda *a, **k: FakeState(tmp_path))
    bound = {}

    def fake_bind(**kw):
        bound.update(kw)
        return ti.InterfaceOutcome(True, "state_write", {}, {})
    monkeypatch.setattr(ti, "set_trae_workspace", fake_bind)
    return ws, bound


def _pages_setup(monkeypatch, pages_after):
    old = {"id": "OLD", "type": "page", "title": "other - TRAE CN [管理员]"}
    calls = {"n": 0}

    def fake_targets(port):
        calls["n"] += 1
        return [old] if calls["n"] == 1 else [old, *pages_after]
    monkeypatch.setattr(cdp_mod, "_targets", fake_targets)
    return old


def test_new_cdp_page_accepted_without_cache(gate_env, monkeypatch):
    ws, bound = gate_env
    new = {"id": "NEW", "type": "page", "title": "workspace - TRAE CN [管理员]"}
    _pages_setup(monkeypatch, [new])
    out = ti.open_trae_workspace(workspace=str(ws), timeout_seconds=3)
    assert out.ok, out.to_dict()
    assert out.actual["page_id"] == "NEW"
    assert out.actual["cdp_page"] is True
    assert bound["workspace"] == str(ws)


def test_old_page_with_same_title_not_accepted(gate_env, monkeypatch):
    ws, _ = gate_env
    stale = {"id": "OLD2", "type": "page", "title": "workspace - TRAE CN [管理员]"}
    old = {"id": "OLD", "type": "page", "title": "other - TRAE CN"}
    monkeypatch.setattr(cdp_mod, "_targets", lambda port: [old, stale])
    out = ti.open_trae_workspace(workspace=str(ws), timeout_seconds=1)
    assert not out.ok
    assert out.error_code == "WORKSPACE_MISMATCH"


def test_no_new_page_still_mismatch(gate_env, monkeypatch):
    ws, _ = gate_env
    _pages_setup(monkeypatch, [])
    out = ti.open_trae_workspace(workspace=str(ws), timeout_seconds=1)
    assert not out.ok
    assert out.error_code == "WORKSPACE_MISMATCH"


def test_already_open_workspace_short_circuits(gate_env, monkeypatch):
    ws, _ = gate_env
    monkeypatch.setattr(ti, "current_trae_workspace", lambda *a, **k: str(ws))
    out = ti.open_trae_workspace(workspace=str(ws), timeout_seconds=1)
    assert out.ok
    assert out.actual.get("already_open") is True