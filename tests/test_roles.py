"""Role policy: real-catalog resolution, pinning, escalation, importers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentloop_hub.control.roles import (
    Candidate, RolePolicy, classify_model, policy_from_agents_md, policy_from_opencode,
)
from agentloop_hub.errors import ValidationError
from agentloop_hub.models import AgentRecord


def agent(agent_id: str, models: list[str], *, caps: dict | None = None,
          adapter: str = "cli", status: str = "online", level: str = "L3") -> AgentRecord:
    return AgentRecord(
        agent_id=agent_id, name=agent_id, product=agent_id, adapter_type=adapter,
        access_level=level, status=status, models=list(models),
        adapter_config=({"gui_app": agent_id} if adapter == "gui_automation"
                        else {"command": [agent_id]}),
        capabilities=caps or {
            "read_files": True, "write_files": True, "terminal": True,
            "structured_output": True, "web_search": True,
            "session_read": False, "session_write": False, "mcp": False,
        },
    )


CATALOG = {
    "local_runner": ["qwen-local/Qwen3.8-27B-Q8.gguf"],
    "free_cli": ["vendor/flash-free", "vendor/glm-flash"],
    "points_gui": ["DeepSeek-Flash", "deepseek-v4-pro"],
}


def agents() -> list[AgentRecord]:
    return [
        agent("local_runner", CATALOG["local_runner"]),
        agent("free_cli", CATALOG["free_cli"]),
        agent("points_gui", CATALOG["points_gui"], adapter="gui_automation"),
    ]


def test_classify_model_separates_local_free_cheap_and_strong():
    assert classify_model("qwen-local/Qwen3.8-27B-Q8.gguf") == "local"
    assert classify_model("vendor/flash-free") == "free"
    assert classify_model("DeepSeek-Flash") == "cheap"
    assert classify_model("deepseek-v4-pro") == "strong"
    assert classify_model("glm-5.2") == "standard"
    assert classify_model("") == "standard"


def test_default_policy_has_the_eight_roles_the_requirement_needs():
    policy = RolePolicy.default()
    assert set(policy.roles) == {
        "explore", "research", "implement", "review", "plan", "write",
        "verify", "summarize",
    }
    # cost ladder must start cheap for the execution roles
    assert policy.roles["implement"].ladder[0].tier <= 1
    assert policy.roles["summarize"].ladder[0].tier >= 3


def test_candidates_only_come_from_the_real_catalog():
    policy = RolePolicy.default()
    for role in policy.roles:
        for candidate in policy.candidates(role, agents=agents(), catalog=CATALOG):
            assert candidate.model in CATALOG[candidate.agent_id]


def test_a_role_with_no_matching_model_resolves_to_nothing():
    policy = RolePolicy.default()
    catalog = {"local_runner": ["qwen-local/Qwen3.8-27B-Q8.gguf"]}
    only_local = [agent("local_runner", catalog["local_runner"])]
    # review's ladder starts at "cheap"; a local checkpoint is not a substitute,
    # so the role reports no candidate instead of inventing one.
    assert policy.candidates("review", agents=only_local, catalog=catalog) == []
    # ...while explore, whose first rung *is* local, resolves.
    assert policy.candidates("explore", agents=only_local, catalog=catalog)


def test_offline_agents_are_never_suggested():
    policy = RolePolicy.default()
    offline = [agent("free_cli", CATALOG["free_cli"], status="offline")]
    assert policy.candidates("implement", agents=offline, catalog=CATALOG) == []


def test_session_pin_goes_first_and_is_validated_against_the_catalog():
    policy = RolePolicy.default().with_overrides(
        executor=("points_gui", "DeepSeek-Flash"),
        commander=("points_gui", "deepseek-v4-pro"),
        catalog=CATALOG,
    )
    impl = policy.candidates("implement", agents=agents(), catalog=CATALOG)
    assert (impl[0].agent_id, impl[0].model) == ("points_gui", "DeepSeek-Flash")
    assert impl[0].tier == 2
    plan = policy.candidates("plan", agents=agents(), catalog=CATALOG)
    assert (plan[0].agent_id, plan[0].model) == ("points_gui", "deepseek-v4-pro")
    assert plan[0].tier == 4


def test_a_pin_to_a_model_that_does_not_exist_is_rejected():
    with pytest.raises(ValidationError) as exc:
        RolePolicy.default().with_overrides(
            executor=("points_gui", "gpt-9-imaginary"), catalog=CATALOG)
    assert "真实模型清单" in str(exc.value)


def test_escalation_moves_forward_and_never_downgrades_first():
    policy = RolePolicy.default().with_overrides(
        executor=("points_gui", "DeepSeek-Flash"), catalog=CATALOG)
    ladder = policy.candidates("implement", agents=agents(), catalog=CATALOG)
    assert len(ladder) >= 2
    first = ladder[0]
    nxt = policy.escalate("implement", first, agents=agents(), catalog=CATALOG)
    assert nxt is not None and nxt.key != first.key
    assert nxt.tier >= first.tier
    # walking past the end terminates instead of looping
    seen = {first.key, nxt.key}
    current = nxt
    for _ in range(10):
        following = policy.escalate("implement", current, agents=agents(),
                                    catalog=CATALOG, tried=seen)
        if following is None:
            break
        assert following.key not in seen
        seen.add(following.key)
        current = following
    assert len(seen) <= len(ladder)


def test_policy_round_trips_through_json_and_validates_unknown_fields(tmp_path: Path):
    policy = RolePolicy.default()
    path = tmp_path / ".agentloop" / "roles.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    policy.save(path)
    loaded = RolePolicy.load(tmp_path)
    assert loaded.name == policy.name
    assert {k: v.to_dict() for k, v in loaded.roles.items()} == \
           {k: v.to_dict() for k, v in policy.roles.items()}

    bad = json.loads(path.read_text(encoding="utf-8"))
    bad["roles"]["implement"]["unexpected"] = True
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValidationError):
        RolePolicy.load(tmp_path)


def test_project_overlay_replaces_a_role_but_keeps_the_others(tmp_path: Path):
    base = RolePolicy.default()
    overlay = {
        "roles": {
            "implement": {
                "task_type": "implementation", "write": True,
                "ladder": [{"tier": 1, "agents": ["free_cli"], "models": ["vendor/flash-free"]}],
            }
        }
    }
    policy = base.merged(overlay, source="test")
    assert policy.roles["implement"].ladder[0].models == ["vendor/flash-free"]
    assert policy.roles["review"].ladder == base.roles["review"].ladder


def test_explain_reports_status_per_role():
    policy = RolePolicy.default()
    report = policy.explain(agents=agents(), catalog=CATALOG)
    assert {row["role"] for row in report["roles"]} == set(policy.roles)
    for row in report["roles"]:
        assert row["status"] in ("ready", "no_candidate")
        if row["resolved"]:
            assert row["resolved"][0]["agent_id"] in CATALOG


OPENCODE_SAMPLE = {
    "$schema": "https://opencode.ai/config.json",
    "agent": {
        "build": {"description": "L1", "model": "vendor/flash-free"},
        "explore": {"mode": "subagent", "model": "local/qwen-local/Qwen3.8-27B-Q8.gguf"},
        "plan": {"model": "vendor/deepseek-v4-pro"},
        "reviewer": {"model": "vendor/deepseek-v4-pro"},
        "writer": {"model": "vendor/glm-flash"},
        "compaction": {"model": "vendor/not-installed-anywhere"},
    },
}


def test_opencode_config_is_imported_as_role_preferences(tmp_path: Path):
    path = tmp_path / "opencode.json"
    path.write_text(json.dumps(OPENCODE_SAMPLE), encoding="utf-8")
    parsed = policy_from_opencode(path, catalog=CATALOG)
    roles = {row["role"] for row in parsed["matched"]}
    # compaction maps to "summarize" but its model is not installed, so that
    # role produces no rung instead of a phantom one.
    assert {"implement", "explore", "plan", "review", "write"} <= roles
    assert "summarize" not in (parsed["roles"] or {})
    assert ("free_cli", "vendor/flash-free") in {
        (row["agent_id"], row["model"]) for row in parsed["matched"]
    }
    # a model that is not installed locally must be reported, never invented
    assert "vendor/not-installed-anywhere" in parsed["unmatched"]
    assert parsed["roles"]["implement"]["ladder"][0]["tier"] == 1


AGENTS_MD_SAMPLE = """# Build Agent 工作策略

## 子 Agent 模型路由

### Explore

模型：

`local/qwen-local/Qwen3.8-27B-Q8.gguf`

### Research

默认模型：

`vendor/flash-free`

升级条件：`vendor/deepseek-v4-pro`

### Reviewer

默认模型：

`vendor/deepseek-v4-pro`
"""


def test_agents_md_is_parsed_into_role_preferences(tmp_path: Path):
    path = tmp_path / "AGENTS.md"
    path.write_text(AGENTS_MD_SAMPLE, encoding="utf-8")
    parsed = policy_from_agents_md(path, catalog=CATALOG)
    assert parsed["roles_found"] == ["explore", "research", "review"]
    explore = parsed["roles"]["explore"]["ladder"]
    assert explore[0]["models"] == ["qwen-local/Qwen3.8-27B-Q8.gguf"]
    research_ladder = parsed["roles"]["research"]["ladder"]
    assert research_ladder[0]["models"] == ["vendor/flash-free"]
    # "vendor/deepseek-v4-pro" resolves to the installed "deepseek-v4-pro"
    assert research_ladder[1]["models"] == ["deepseek-v4-pro"]
    assert research_ladder[1]["tier"] == 4
    # the builtin rungs survive behind the imported preferences
    assert any("model_class" in rung for rung in research_ladder)
    assert parsed["unmatched"] == []


def test_imported_policy_still_falls_back_to_the_builtin_ladder(tmp_path: Path):
    path = tmp_path / "opencode.json"
    path.write_text(json.dumps(OPENCODE_SAMPLE), encoding="utf-8")
    parsed = policy_from_opencode(path, catalog=CATALOG)
    merged = RolePolicy.default().merged({"roles": parsed["roles"]}, source="import")
    candidates = merged.candidates("implement", agents=agents(), catalog=CATALOG)
    assert candidates[0].agent_id == "free_cli"
    # the builtin rungs are still behind the imported preference
    assert len(candidates) > 1


def test_candidate_serialises_its_cost_label():
    candidate = Candidate(role="implement", tier=2, agent_id="a", agent_name="A",
                          model="DeepSeek-Flash")
    payload = candidate.to_dict()
    assert payload["tier_label"] == "cheap"
    assert payload["model"] == "DeepSeek-Flash"
    assert candidate.key == "a::DeepSeek-Flash"
