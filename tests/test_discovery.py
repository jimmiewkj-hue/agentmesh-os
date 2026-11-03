from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agentloop_hub.adapters.discovery import DiscoveryContext, discover_agents


def touch(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_discovery_finds_all_supported_agents(tmp_path, monkeypatch):
    home = tmp_path / "home"
    appdata = tmp_path / "appdata"
    local = tmp_path / "local"
    pf = tmp_path / "pf"
    ctx = DiscoveryContext(home=home, appdata=appdata, localappdata=local, programfiles=pf)

    touch(local / "Programs" / "Trae" / "Trae.exe")
    touch(local / "Programs" / "Trae" / "resources" / "app" / "package.json",
          json.dumps({"version": "1.2.3"}))
    touch(appdata / "Trae" / "ModularData" / "ai-agent" / "database.db", "db")
    touch(local / "Programs" / "Trae CN" / "Trae CN.exe")
    touch(appdata / "Trae CN" / "ModularData" / "ai-agent" / "database.db", "db")
    touch(pf / "Qoder" / "Qoder" / "Qoder.exe")
    touch(pf / "Qoder" / "Qoder CN" / "Qoder CN.exe")
    touch(pf / "ZCode" / "ZCode.exe")
    touch(pf / "WorkBuddy" / "WorkBuddy.exe")
    touch(pf / "WorkBuddy" / "version", "9.9.9")
    touch(local / "Programs" / "DeepSeek Harness" / "DeepSeek Harness.exe")
    touch(local / "Programs" / "DeepSeek Harness" / "version", "44.1.0")
    (home / ".qoder" / "projects" / "p1").mkdir(parents=True)
    touch(home / ".qoder" / "projects" / "p1" / "s.jsonl",
          json.dumps({"type": "runtime-config", "model": "q-model"}) + "\n")
    (home / ".qoder-cn" / "projects").mkdir(parents=True)
    (home / ".zcode" / "cli" / "rollout").mkdir(parents=True)
    touch(home / ".zcode" / "cli" / "rollout" / "model-io-sess_a.jsonl",
          json.dumps({"model": {"modelId": "z-model"}}) + "\n")
    (home / ".workbuddy" / "projects" / "p").mkdir(parents=True)
    touch(home / ".workbuddy" / "projects" / "p" / "s.jsonl",
          json.dumps({"providerData": {"model": "wb-model"}}) + "\n")
    (home / ".dsh" / "sessions" / "p").mkdir(parents=True)

    fake_open = tmp_path / "opencode.exe"
    fake_open.write_text("", encoding="utf-8")
    monkeypatch.setattr("agentloop_hub.adapters.discovery.resolve_opencode_executable",
                        lambda: str(fake_open))
    monkeypatch.setattr("agentloop_hub.adapters.discovery._opencode_version", lambda exe: "9.0.0")

    agents = discover_agents("prj_1", ctx)
    by_id = {a.agent_id: a for a in agents}
    assert set(by_id) == {"trae", "trae_cn", "qoder", "qoder_cn", "zcode",
                          "workbuddy", "dsh", "opencode", "claude_code",
                          "gemini_cli", "codex_cli", "aider", "cursor"}
    assert by_id["trae"].version == "1.2.3"
    assert by_id["workbuddy"].version == "9.9.9"
    assert by_id["dsh"].version == "44.1.0"
    assert by_id["opencode"].access_level == "L3"
    assert by_id["opencode"].capabilities["structured_output"] is True
    assert by_id["trae"].access_level == "L2"
    assert by_id["trae"].capabilities["session_read"] is True
    assert by_id["qoder"].models == ["q-model"]
    assert by_id["zcode"].models == ["z-model"]
    assert by_id["workbuddy"].models == ["wb-model"]
    for agent in agents:
        assert agent.project_id == "prj_1"
        assert agent.discovered is True


def test_model_detection_from_real_config_formats(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ctx = DiscoveryContext(home=home, appdata=tmp_path / "a", localappdata=tmp_path / "l",
                           programfiles=tmp_path / "p", path_lookup=lambda name: None)
    touch(home / ".zcode" / "v2" / "provider_config.json", json.dumps({
        "config": {"providerConfigRules": {"providerRules": [
            {"providerId": "p1", "config": {"personalModelIds": ["zc-a", "zc-b"]}},
        ]}},
    }))
    touch(home / ".dsh" / "settings.yaml",
          "llm-pi-ai:\n  providers:\n    p:\n      models:\n        - id: dsh-a\n"
          "agent-default-model:\n  provider: p\n  model: dsh-b\n")
    touch(home / ".workbuddy" / "models.json", json.dumps(["wb-a", "wb-b"]))
    monkeypatch.setattr("agentloop_hub.adapters.discovery.resolve_opencode_executable",
                        lambda: "")
    agents = {a.agent_id: a for a in discover_agents("prj_1", ctx)}
    assert set(agents["zcode"].models) >= {"zc-a", "zc-b"}
    assert set(agents["dsh"].models) >= {"dsh-a", "dsh-b"}
    assert set(agents["workbuddy"].models) >= {"wb-a", "wb-b"}


def test_discovery_registers_rea_mcp_from_project_config(tmp_path):
    home = tmp_path / "home"
    rea = tmp_path / "rea"
    touch(rea / ".mcp.json", json.dumps({
        "mcpServers": {"rea": {
            "command": sys.executable, "args": ["rea.mjs", "mcp"],
            "env": {"REA_ANALYSIS_PROVIDER": "ghidra"},
        }},
    }))
    touch(rea / ".rea-cli" / "node_modules" / "rea-agents" / "package.json",
          json.dumps({"name": "rea-agents", "version": "6.8.0"}))
    ctx = DiscoveryContext(home=home, appdata=tmp_path / "a", localappdata=tmp_path / "l",
                           programfiles=tmp_path / "p", path_lookup=lambda name: None,
                           rea_home=rea)
    agents = {a.agent_id: a for a in discover_agents("prj_1", ctx)}
    assert "rea" in agents
    agent = agents["rea"]
    assert agent.adapter_type == "mcp"
    assert agent.version == "6.8.0"
    assert agent.adapter_config["mode"] == "mcp"
    assert agent.capabilities["local_mcp"] is True


def test_discovery_reports_offline_without_installs(tmp_path, monkeypatch):
    ctx = DiscoveryContext(home=tmp_path / "h", appdata=tmp_path / "a",
                           localappdata=tmp_path / "l", programfiles=tmp_path / "p",
                           path_lookup=lambda name: None)
    monkeypatch.setattr("agentloop_hub.adapters.discovery.resolve_opencode_executable",
                        lambda: "")
    agents = discover_agents("prj_1", ctx)
    assert len(agents) == 13
    assert all(a.status == "offline" for a in agents)
