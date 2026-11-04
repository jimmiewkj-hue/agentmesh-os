"""Independent requirement verification for AgentMesh OS.

Every test maps to one statement of ``01-需求整理-v1.docx`` (the declared
requirement baseline). This suite is written from the requirement text only and
does not import the project's own test helpers, so a green result here is
independent evidence rather than a re-run of ``tests/``.

Naming convention:
    test_Rn_...   requirement statement expected to hold
    test_GAP_...  deliberate gap probe; PASSES when the gap is confirmed
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import DEFAULT_SUMMARY, plan, task

from agentloop_hub import connectors
from agentloop_hub.config import HubConfig
from agentloop_hub.errors import ApprovalRequiredError, ValidationError
from agentloop_hub.models import AgentRecord


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _fix_scenario(*task_ids: str, write_fix: bool = False) -> dict:
    data = {}
    for task_id in task_ids:
        action = {
            "summary": f"{task_id} completed",
            "evidence": [{"file": "calc.py", "lines": "1-2", "description": "verified"}],
            "findings": [{"severity": "info", "summary": f"{task_id} inspected"}],
            "tests": [{"command": "python -m pytest -q", "result": "passed"}],
        }
        if write_fix and task_id in ("T002", "T001"):
            action["write_files"] = [
                {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
            ]
        data[task_id] = action
    return data


def _approval_id(hub, kind: str, **match) -> str:
    for item in hub.approvals_all(limit=500):
        if item["kind"] != kind:
            continue
        if all(item.get(key) == value for key, value in match.items()):
            return item["approval_id"]
    raise AssertionError(f"未找到审批: kind={kind} match={match}")


def _read_pkg(row: dict) -> dict:
    return json.loads((Path(row["task_dir"]) / "task.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# R1  主节点不是 Agent，而是 Controller
# ---------------------------------------------------------------------------
def test_R1_main_node_is_never_routed_as_executor(env):
    preview = env.hub.route_preview(task_type="implementation")
    assert preview["selected"]["agent_id"] != "mock_main"
    rejected = {c["agent_id"]: c.get("rejected", []) for c in preview["candidates"]
                if c.get("rejected")}
    assert "mock_main" in rejected, preview


def test_R1_control_plane_contains_no_llm_client():
    """Controller 必须是确定性软件：控制平面不得调用任何 LLM 或远端 API。

    本机 127.0.0.1 的 CDP 探活属于 GUI 桥接，不是控制决策，允许存在。
    """
    import re

    root = Path(__file__).resolve().parents[1] / "agentloop_hub" / "control"
    llm_tokens = ("openai", "anthropic", "chat/completions", "api_key", "bearer ")
    url_re = re.compile(r"https?://[^\s\"'\)]+")
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        lowered = text.lower()
        for token in llm_tokens:
            if token in lowered:
                offenders.append(f"{path.name}:llm:{token}")
        for url in url_re.findall(text):
            host = url.split("/")[2].split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                offenders.append(f"{path.name}:remote:{url}")
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# R2  Agent Connector：统一 {agent_id, capability[]}
# ---------------------------------------------------------------------------
def test_R2_manifest_exposes_unified_agent_and_capability(env):
    manifest = env.hub.connector_manifest()
    assert manifest["protocol"] == "agentmesh.connector.v1"
    ids = {a["agent_id"] for a in manifest["agents"]}
    assert {"mock_main", "mock_worker"} <= ids
    vocabulary = set(manifest["capability_vocabulary"])
    for agent in manifest["agents"]:
        assert agent["agent_id"]
        assert isinstance(agent["capability"], list)
        assert set(agent["capability"]) <= vocabulary, agent["capability"]


def test_R2_all_products_named_by_requirement_are_profiled():
    required = {
        "trae", "qoder", "workbuddy", "opencode", "zcode", "dsh",
        "cursor", "claude_code", "gemini_cli", "codex_cli", "aider",
    }
    profiled = {p.agent_id for p in connectors.CONNECTOR_PROFILES}
    assert required <= profiled, required - profiled


def test_R2_offline_relay_agent_reports_no_capability(env):
    env.hub.register_agent({
        "agent_id": "ghost_relay", "name": "Ghost", "adapter_type": "file_relay",
        "status": "offline",
        "capabilities": {k: False for k in (
            "read_files", "write_files", "terminal", "web_search", "mcp",
            "session_read", "session_write", "structured_output")},
    })
    descriptor = next(a for a in env.hub.connector_manifest()["agents"]
                      if a["agent_id"] == "ghost_relay")
    assert descriptor["capability"] == []


# ---------------------------------------------------------------------------
# R3  Unified Memory：Level 1-5 与启动引导包
# ---------------------------------------------------------------------------
def test_R3_five_sync_levels_are_materialised(env):
    status = env.hub.sync_status()
    assert set(status["levels"]) == {1, 2, 3, 4, 5}
    labels = " ".join(v["label"] for v in status["levels"].values())
    for keyword in ("消息", "任务", "上下文", "知识", "工作流"):
        assert keyword in labels, labels
    # 项目索引/代码结构在打开工作区时就已经写入 Level 3
    assert status["levels"][3]["memory_records"] >= 2


def test_R3_bootstrap_is_shared_by_every_agent(env):
    first = env.hub.bootstrap(agent_id="mock_main")
    second = env.hub.bootstrap(agent_id="mock_worker")
    assert first["project"]["root"] == second["project"]["root"] == str(env.repo)
    assert set(first["groups"]) == {
        "project_index", "code_structure", "task_state", "workflow",
        "knowledge", "decision", "message", "user_preference",
    }
    assert len(first["sync_levels"]) == 5
    assert first["groups"]["project_index"], first["groups"]["project_index"]


def test_R3_bootstrap_is_injected_into_the_task_package(env):
    env.set_scenario(_fix_scenario("T001"))
    run = env.run_plan(plan([task("T001", kind="analysis", agent="mock_worker")]))
    row = env.task_rows(run["run_id"])[0]
    package = _read_pkg(row)
    assert package["bootstrap"]["project"]["root"] == str(env.repo)
    assert package["bootstrap"]["groups"], package["bootstrap"].keys()


# ---------------------------------------------------------------------------
# R4  Context Bus：所有信息流经总线，只追加 + 哈希链 + 游标
# ---------------------------------------------------------------------------
def test_R4_bus_accepts_exactly_the_required_event_families(env):
    for kind in ("prompt", "response", "file_change", "task_state",
                 "knowledge", "workflow"):
        published = env.hub.bus_publish(kind, {"probe": kind})
        assert published["kind"] == kind
    with pytest.raises(ValidationError):
        env.hub.bus_publish("log", {"x": 1})


def test_R4_bus_is_append_only_with_intact_hash_chain(env):
    env.hub.bus_publish("knowledge", {"title": "append-only probe"})
    db_path = env.repo / ".agentloop" / "state.db"
    connection = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE bus_events SET kind='tampered' WHERE seq=1")
            connection.commit()
    finally:
        connection.close()
    verdict = env.hub.bus_verify()
    assert verdict["ok"] is True, verdict


def test_R4_subscription_cursor_and_replay(env):
    subscription = env.hub.bus_subscribe("verify-reader", kinds=["knowledge"])
    for index in range(3):
        env.hub.bus_publish("knowledge", {"index": index})
    first = env.hub.bus_poll(subscription["subscription_id"])
    assert first["count"] >= 3
    second = env.hub.bus_poll(subscription["subscription_id"])
    assert second["count"] == 0, "游标未推进，会重复投递"
    replayed = env.hub.bus_events(after_seq=0, kind="knowledge")
    assert replayed["count"] >= 3


# ---------------------------------------------------------------------------
# R5  Task Scheduler：依赖 / 优先级 / 重试 / 熔断
# ---------------------------------------------------------------------------
def test_R5_dependency_graph_orders_execution(env):
    env.set_scenario(_fix_scenario("T001", "T002", write_fix=True))
    run = env.run_plan(plan([
        task("T001", kind="analysis", agent="mock_worker", priority=3),
        task("T002", kind="implementation", agent="mock_worker", deps=["T001"],
             scope=["calc.py"], out_of_scope=["README.md"], verify_cmds=[]),
    ]))
    rows = {r["task_id"]: r for r in env.task_rows(run["run_id"])}
    assert rows["T001"]["status"] == "completed", rows["T001"]["error"]
    assert rows["T002"]["status"] == "completed", rows["T002"]["error"]
    assert rows["T002"]["started_at"] >= rows["T001"]["finished_at"]


def test_R5_failed_attempt_is_retried(env):
    env.register("flaky", fail_times=1)
    env.set_scenario(_fix_scenario("T001"))
    run = env.run_plan(plan([
        task("T001", kind="analysis", agent="flaky", retry_limit=1),
    ]))
    row = env.task_rows(run["run_id"])[0]
    assert row["status"] == "completed", row["error"]
    assert row["attempts"] == 2


def test_R5_circuit_breaker_stops_the_run(env):
    env.register("always_fail", fail_times=99)
    env.set_scenario({tid: {"exit_code": 1} for tid in ("T001", "T002", "T003")})
    run = env.run_plan(plan([
        task(tid, kind="analysis", agent="always_fail", retry_limit=0)
        for tid in ("T001", "T002", "T003")
    ]), parallel=2)
    status = env.hub.run_status(run["run_id"])
    assert status["run"]["status"] == "paused", status["run"]
    assert "阈值" in str(status["run"].get("error", ""))


# ---------------------------------------------------------------------------
# R6  Capability Router：能力硬门槛 + 多维评分 + 决策留痕
# ---------------------------------------------------------------------------
def test_R6_capability_gate_blocks_agents_without_the_capability(env):
    preview = env.hub.route_preview(task_type="analysis", required=["web_search"])
    assert preview["selected"] is None


def test_GAP_router_drops_all_rejection_reasons_when_nobody_qualifies(env):
    """无人满足能力门槛时，候选与淘汰原因被整体丢弃，路由不可解释。"""
    preview = env.hub.route_preview(task_type="analysis", required=["web_search"])
    assert preview["selected"] is None
    assert preview["candidates"] == [], preview["candidates"]


def test_R6_scoring_uses_capability_success_verification_speed_cost(env):
    scores = env.hub.router_scores()
    assert {"capability", "success", "verification", "speed", "cost"} <= set(scores["weights"])


def test_R6_every_dispatch_is_recorded_with_reason(env):
    env.set_scenario(_fix_scenario("T001"))
    run = env.run_plan(plan([task("T001", kind="analysis", agent="mock_worker")]))
    decisions = env.hub.routing_decisions(run_id=run["run_id"])
    assert decisions, "路由决策未留痕"
    decision = decisions[0]
    assert decision["agent_id"] == "mock_worker"
    assert "capability" in decision["breakdown"]
    assert decision["reason"]


# ---------------------------------------------------------------------------
# R7  Cost Optimizer：压缩 / 缓存 / 结果复用 / 评测
# ---------------------------------------------------------------------------
def test_R7_context_compression_shrinks_and_records_savings(env):
    oversized = {"findings": [{"detail": "x" * 400} for _ in range(80)]}
    compressed = env.hub.cost.compress_context(oversized, 500)
    assert len(json.dumps(compressed, ensure_ascii=False)) < len(
        json.dumps(oversized, ensure_ascii=False))
    kinds = {row["kind"] for row in env.hub.cost_report()["savings"]["by_kind"]}
    assert "context_compression" in kinds, kinds


def test_R7_semantic_or_exact_cache_hits_on_repeat_analysis(env):
    env.set_scenario(_fix_scenario("T001"))
    blueprint = plan([task("T001", kind="analysis", agent="mock_worker", scope=["calc.py"])])
    env.run_plan(blueprint)
    second = env.run_plan(blueprint)
    report = env.hub.cost_report(run_id=second["run_id"])
    kinds = {row["kind"] for row in report["savings"]["by_kind"]}
    assert kinds & {"exact_cache", "semantic_cache"}, report["savings"]


def test_R7_benchmark_reports_success_verification_cost_speed(env):
    env.set_scenario(_fix_scenario("T001"))
    env.run_plan(plan([task("T001", kind="analysis", agent="mock_worker")]))
    bench = env.hub.benchmark()
    row = next(r for r in bench["agents"] if r["agent_id"] == "mock_worker")
    assert {"success_rate", "verification_pass_rate", "avg_cost", "avg_duration_ms",
            "score"} <= set(row)
    assert bench["cheapest_sufficient"]


# ---------------------------------------------------------------------------
# R8  Human Governance：四道人类门禁
# ---------------------------------------------------------------------------
def test_R8_architecture_task_requires_architecture_approval_first(env):
    env.set_scenario(_fix_scenario("T001"))
    result = env.hub.import_plan(plan([
        task("T001", kind="architecture", agent="mock_worker"),
    ]))
    assert result["architecture_approval_id"]
    with pytest.raises(ApprovalRequiredError):
        env.hub.decide_approval(result["approval_id"], approve=True)
    env.hub.decide_approval(result["architecture_approval_id"], approve=True)
    approved = env.hub.decide_approval(result["approval_id"], approve=True)
    assert approved["outcome"]["run_id"]


def test_R8_merge_is_blocked_before_acceptance_and_without_approval(env):
    env.set_scenario(_fix_scenario("T001", "T002", write_fix=True))
    run = env.run_plan(plan([
        task("T001", kind="implementation", agent="mock_worker",
             scope=["calc.py"], out_of_scope=["README.md"]),
    ]))
    status = env.hub.run_status(run["run_id"])["run"]
    assert status["status"] == "pending_acceptance", status

    with pytest.raises(ValidationError):
        env.hub.merge(run["run_id"])

    acceptance = _approval_id(env.hub, "acceptance", run_id=run["run_id"])
    env.hub.decide_approval(acceptance, approve=True)
    assert env.hub.run_status(run["run_id"])["run"]["status"] == "pending_merge"

    with pytest.raises(ApprovalRequiredError):
        env.hub.merge(run["run_id"])


def test_R8_full_gate_chain_accept_merge_release(env):
    env.set_scenario(_fix_scenario("T001", write_fix=True))
    run = env.run_plan(plan([
        task("T001", kind="implementation", agent="mock_worker",
             scope=["calc.py"], out_of_scope=["README.md"]),
    ]))
    run_id = run["run_id"]
    assert env.hub.run_status(run_id)["run"]["status"] == "pending_acceptance"

    with pytest.raises(ValidationError):
        env.hub.request_release(run_id)

    env.hub.decide_approval(_approval_id(env.hub, "acceptance", run_id=run_id), approve=True)
    merge_approval = _approval_id(env.hub, "merge", run_id=run_id)
    env.hub.decide_approval(merge_approval, approve=True)
    assert env.hub.run_status(run_id)["run"]["status"] == "completed"

    assert "return a + b" in (env.repo / "calc.py").read_text(encoding="utf-8")

    release = env.hub.request_release(run_id)
    assert release["status"] == "pending_approval"
    env.hub.decide_approval(release["approval_id"], approve=True)
    final = env.hub.run_status(run_id)["run"]
    assert final["status"] == "released"
    assert final["release_status"] == "approved"


# ---------------------------------------------------------------------------
# R9  用户只面对一个入口
# ---------------------------------------------------------------------------
def test_R9_one_goal_is_enough_to_generate_a_plan(env):
    env.set_plan(plan([
        task("T001", kind="analysis", agent="mock_worker",
             objective="扫描 calc.py 并报告问题"),
    ], goal="帮我检查这个项目"))
    answer = env.hub.ask("帮我检查这个项目")
    assert answer["status"] == "pending_approval", answer
    assert answer["plan"]["tasks"][0]["task_id"] == "T001"
    assert env.hub.approvals_pending()


def test_R9_http_single_entry_accepts_one_goal(env):
    from agentloop_hub.api.server import JobManager, _make_handler

    env.set_plan(plan([task("T001", kind="analysis", agent="mock_worker")],
                      goal="HTTP 单入口验证"))
    handler = _make_handler(env.hub, JobManager(), None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/ask",
            data=json.dumps({"goal": "HTTP 单入口验证"}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["status"] == "pending_approval", payload
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview", timeout=30) as r:
            overview = json.loads(r.read().decode("utf-8"))
        assert overview["project"]["root"] == str(env.repo)
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# R10 产品愿景：用户不感知其它 Agent（自动执行覆盖率）
# ---------------------------------------------------------------------------
def test_GAP_most_products_still_need_a_human_in_the_loop():
    """需求愿景是「用户不感知」，实测：13 个产品里只有 5 个可无人执行。"""
    records = [
        AgentRecord(
            agent_id=profile.agent_id, name=profile.product, product=profile.product,
            edition=profile.edition, project_id="p", version="1",
            adapter_type=profile.adapter_type, adapter_config={},
            access_level=profile.access_level_if_installed, capabilities={},
            models=[], permissions={}, status="online", discovered=True,
        )
        for profile in connectors.CONNECTOR_PROFILES
    ]
    automatic = sorted(r.agent_id for r in records if connectors.is_auto_executable(r))
    assert automatic == ["aider", "claude_code", "codex_cli", "gemini_cli", "opencode"]
    manual = sorted(set(r.agent_id for r in records) - set(automatic))
    assert len(manual) == 9, manual


def test_GAP_offline_cli_agent_is_still_marked_auto_executable():
    """离线判定只影响状态，不影响 auto_executable 能力标记。"""
    record = AgentRecord(
        agent_id="offline_cli", name="Offline CLI", product="x", edition="",
        project_id="p", version="1", adapter_type="cli", adapter_config={},
        access_level="L3", capabilities={k: False for k in (
            "read_files", "write_files", "terminal", "web_search", "mcp",
            "session_read", "session_write", "structured_output")},
        models=[], permissions={}, status="offline", discovered=True,
    )
    assert connectors.is_auto_executable(record) is True
    assert connectors.derive_capability_list(record) == ["auto_executable"]


# ---------------------------------------------------------------------------
# R11 可观测性：审计链、版本台账
# ---------------------------------------------------------------------------
def test_R11_audit_chain_and_version_ledger_survive_a_run(env):
    env.set_scenario(_fix_scenario("T001"))
    env.run_plan(plan([task("T001", kind="analysis", agent="mock_worker")]))
    assert env.hub.audit_verify()["ok"] is True
    events = env.hub.audit_events(limit=500)
    assert any(e["action"] == "task.executed" for e in events)
    versions = env.hub.agent_versions()
    assert versions["count"] >= 1
    assert any(v["agent_id"] == "mock_worker" for v in versions["versions"])


# ---------------------------------------------------------------------------
# R12 声明真实性
# ---------------------------------------------------------------------------
def test_GAP_independent_verifier_executes_in_the_live_workspace(env):
    """独立验证 Agent 以项目主工作区为 cwd 执行，不受 Worktree 隔离。

    需求与 README 都声称「子任务在独立 Worktree 中执行」；实测验证节点直接
    在用户工作区运行，Mock 验证器写入的 CANARY 文件落进了项目根目录。
    """
    env.set_scenario({
        "T001": {"summary": "probe", "evidence": [{"file": "calc.py", "lines": "1",
                                                   "description": "probe"}]},
        "__verify__": {"write_files": [{"path": "VERIFIER_CANARY.txt",
                                        "content": "written by verifier"}]},
    })
    env.run_plan(plan([task("T001", kind="analysis", agent="mock_worker")]))
    assert (env.repo / "VERIFIER_CANARY.txt").exists()


def test_GAP_overview_module_status_is_a_hardcoded_literal(env):
    """`/api/overview` 的 modules.ready 是写死的，不反映真实健康度。"""
    before = env.hub.overview()["modules"]
    for agent in env.hub.list_agents():
        env.hub.remove_agent(agent.agent_id)
    after = env.hub.overview()["modules"]
    assert before == after
    assert set(after.values()) == {"ready"}


def test_GAP_cost_in_usd_cannot_be_measured_out_of_the_box(env):
    """定价表为空 -> 美元成本恒为 0；「100 美元降到 10 美元」无法量化。"""
    config = HubConfig()
    assert config.pricing == {}
    assert config.price_for("any/model") == {"input": 0.0, "output": 0.0}

    env.hub.cost.compress_context({"findings": [{"detail": "y" * 400} for _ in range(60)]}, 400)
    totals = env.hub.cost_report()["savings"]["totals"]
    assert totals["tokens_saved"] > 0
    assert totals["cost_saved"] == 0
