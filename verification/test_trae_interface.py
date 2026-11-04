"""Independent tests for the Trae / Trae CN zero-input software interface.

Read-only against the real installation; writes only to throw-away copies of
``state.vscdb``.  No application is launched or killed here.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from agentloop_hub.control import trae_interface as trae
from agentloop_hub.control.zero_input import OsInputBlocked, require_os_input

REAL_DB = trae.trae_state_db("trae_cn")
HAS_TRAE = REAL_DB.exists() and trae.trae_executable("trae_cn").exists()

pytestmark = pytest.mark.skipif(not HAS_TRAE, reason="Trae CN 未安装或状态库不存在")


def snapshot(src: Path, dst: Path) -> Path:
    """Consistent copy including WAL frames."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return dst


@pytest.fixture()
def state_copy(tmp_path: Path) -> dict:
    db = snapshot(REAL_DB, tmp_path / "state.vscdb")
    return {"trae_state_db": str(db), "trae_home": str(tmp_path / "home"),
            "executable": str(trae.trae_executable("trae_cn"))}


def test_real_catalog_is_plain_sqlite_and_contains_chain_models():
    probe = trae.probe("trae_cn")
    assert probe["state_db_plain_sqlite"] is True
    assert probe["model_count"] > 0
    deepseek = trae.resolve_trae_model("DeepSeek-V4.1-Flash", app="trae_cn")
    assert deepseek and deepseek["model_id"] == "solo_agent_1__deepseek-v4.1-flash"
    glm = trae.resolve_trae_model("GLM-5.3-Flash", app="trae_cn")
    assert glm and glm["model_id"] == "solo_agent_1__glm-5.3-flash"


def test_unknown_model_is_rejected_not_invented(state_copy):
    outcome = trae.set_trae_model(model="deepseek-v9-imaginary", config=state_copy)
    assert outcome.ok is False
    assert outcome.error_code == "MODEL_UNAVAILABLE"
    assert outcome.actual.get("available")


def test_set_model_persists_and_reads_back(state_copy):
    outcome = trae.set_trae_model(model="DeepSeek-V4.1-Flash", config=state_copy)
    assert outcome.ok, outcome.reason
    state = trae.read_state("trae_cn", state_copy)
    recent = state.get(state.prefixed(
        "AI.agent.model.recent_user_selection_by_agent_label"), {})
    assert recent["solo_agent"]["modelId"] == "solo_agent_1__deepseek-v4.1-flash"
    assert outcome.evidence["keys_written"]
    assert Path(outcome.evidence["backup"]).exists()


def test_set_workspace_binds_session_and_registers_folder(state_copy, tmp_path):
    workspace = tmp_path / "proj" / "chain-demo"
    workspace.mkdir(parents=True)
    outcome = trae.set_trae_workspace(workspace=str(workspace), dev_mode="code",
                                      config=state_copy)
    assert outcome.ok, outcome.reason
    state = trae.read_state("trae_cn", state_copy)
    cache = state.get("solo-lite.project-workspace-cache", {})
    assert any(item["identity"] == str(workspace).lower() for item in cache.values())
    modes = state.get(f"solo-lite-mode-state-map-{state.uid}", {})
    session = modes["code"]["session"]
    assert Path(session["workspacePath"]).resolve() == workspace.resolve()


def test_dev_mode_switch_moves_the_active_session(state_copy, tmp_path):
    workspace = tmp_path / "proj2"
    workspace.mkdir()
    assert trae.set_trae_workspace(workspace=str(workspace), dev_mode="code",
                                   config=state_copy).ok
    outcome = trae.set_trae_workspace(workspace=str(workspace), dev_mode="work",
                                      config=state_copy)
    assert outcome.ok, outcome.reason
    state = trae.read_state("trae_cn", state_copy)
    modes = state.get(f"solo-lite-mode-state-map-{state.uid}", {})
    assert modes["work"]["session"]["mode"] == "work"
    assert modes["code"]["session"] in (None, {})


def test_verify_detects_a_workspace_mismatch(state_copy, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    assert trae.set_trae_model(model="GLM-5.3-Flash", config=state_copy).ok
    outcome = trae.verify_trae_session(
        workspace=str(other), model="GLM-5.3-Flash", dev_mode="code", config=state_copy)
    assert outcome.ok is False
    assert outcome.error_code == "WORKSPACE_MISMATCH"


def test_chat_deeplink_shape_matches_the_verified_handler():
    uri = trae.build_trae_chat_uri(app="trae_cn", prompt="scan repo", mode="solo")
    assert uri.startswith("trae-cn://GitHub.copilot-chat?")
    assert "agent=solo" in uri and "prompt=scan%20repo" in uri


def test_launch_env_never_inherits_electron_run_as_node():
    import os
    os.environ["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        env = trae.clean_launch_env()
        assert "ELECTRON_RUN_AS_NODE" not in env
        assert "NODE_OPTIONS" not in env
    finally:
        os.environ.pop("ELECTRON_RUN_AS_NODE", None)


def test_zero_input_policy_blocks_keyboard_and_mouse():
    with pytest.raises(OsInputBlocked) as exc:
        require_os_input("keyboard")
    assert exc.value.code == "GUI_INPUT_FORBIDDEN"
    with pytest.raises(OsInputBlocked):
        require_os_input("mouse")


@pytest.mark.skipif(os.environ.get("AGENTMESH_TRAE_LIVE") != "1",
                    reason="需要真实 Trae CN 运行且已开 CDP（AGENTMESH_TRAE_LIVE=1）")
def test_live_model_menu_switch_uses_page_level_mouse_events():
    """Live proof: Seed-Code -> DeepSeek-V4.1-Flash through the app's own menu."""
    port = int(os.environ.get("AGENTMESH_TRAE_CDP_PORT", "9342"))
    before = trae.trae_visible_model(port)
    assert before, "输入框模型名读取为空，应用可能未就绪"
    outcome = trae.select_trae_model_via_cdp("DeepSeek-V4.1-Flash", port=port)
    assert outcome.ok, outcome.reason
    assert outcome.actual["visible_model"] == "DeepSeek-V4.1-Flash"
    assert outcome.evidence["source"].startswith("Input.dispatchMouseEvent")
