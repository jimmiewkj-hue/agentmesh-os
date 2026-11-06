"""Deterministic aggregation: digest in, one readable answer out."""

from __future__ import annotations

from agentloop_hub.control.aggregate import (
    apply_polish, build_answer, build_polish_prompt,
)


def task(task_id: str, role: str, *, agent: str = "a", model: str = "m",
         status: str = "completed", verification: str = "passed") -> dict:
    return {
        "task_id": task_id, "title": f"{role} task", "role": role, "type": "analysis",
        "status": status, "verification_status": verification,
        "assigned_agent": agent, "model": model,
    }


def result(task_id: str, *, summary: str, changed: list[str] | None = None,
           unresolved: list | None = None, tests: list | None = None,
           tokens: int = 1000, next_actions: list | None = None) -> dict:
    return {
        "task_id": task_id, "summary": summary, "findings": [{"severity": "low", "summary": "f"}],
        "files_changed": changed or [], "files_read": ["x.py"], "tests": tests or [],
        "evidence": [{"file": "x.py", "lines": "1-2", "description": "e"}],
        "risks": [], "unresolved_items": unresolved or [],
        "recommended_next_action": next_actions or [],
        "token_usage": {"input": tokens, "output": 100},
        "status": "completed",
    }


def test_answer_contains_every_stage_and_the_cost_breakdown():
    answer = build_answer(
        goal="修复 calc.py",
        run={"run_id": "run_1", "status": "pending_acceptance", "acceptance_status": "pending"},
        plan={"goal": "修复 calc.py"},
        tasks=[task("S01", "implement", model="worker-free"),
               task("S02", "verify", agent="v", model="verifier-pro")],
        results={"S01": result("S01", summary="改了 calc.py", changed=["calc.py"]),
                 "S02": result("S02", summary="测试通过")},
    )
    markdown = answer["answer_markdown"]
    assert "# 修复 calc.py" in markdown
    assert "### S01 · implement · completed" in markdown
    assert "### S02 · verify · completed" in markdown
    assert "calc.py" in answer["files_changed"]
    assert answer["cost"]["total_tokens"] == 2200
    assert {row["agent_id"] for row in answer["cost"]["by_agent"]} == {"a", "v"}
    # tier is derived from the real model name, not stored twice
    tiers = {row["tier"]: row["label"] for row in answer["cost"]["by_tier"]}
    assert tiers[1] == "free"
    assert tiers[4] == "strong"


def test_a_failed_stage_is_reported_in_the_conclusion_not_hidden():
    answer = build_answer(
        goal="g", run={"run_id": "r", "status": "failed"}, plan={"goal": "g"},
        tasks=[task("S01", "implement"), task("S02", "verify", status="failed",
                                              verification="failed")],
        results={"S01": result("S01", summary="ok")},
    )
    assert "1 个失败" in answer["conclusion"]
    assert "S02" in answer["conclusion"]
    assert "（无摘要）" in answer["answer_markdown"]


def test_unresolved_and_next_actions_are_unioned_across_stages():
    answer = build_answer(
        goal="g", run={"run_id": "r"}, plan={"goal": "g"},
        tasks=[task("S01", "implement"), task("S02", "review")],
        results={
            "S01": result("S01", summary="a", unresolved=["未跑集成测试"],
                          next_actions=["补 e2e"]),
            "S02": result("S02", summary="b", unresolved=["并发未覆盖"],
                          next_actions=["加锁"]),
        },
    )
    assert set(answer["unresolved"]) == {"未跑集成测试", "并发未覆盖"}
    assert set(answer["next_actions"]) == {"补 e2e", "加锁"}
    assert "## 未解决项" in answer["answer_markdown"]


def test_missing_result_is_explicit_rather_than_fabricated():
    answer = build_answer(goal="g", run={"run_id": "r"}, plan={"goal": "g"},
                         tasks=[task("S01", "implement", status="failed")], results={})
    assert answer["sections"][0]["summary"] == ""
    assert answer["cost"]["total_tokens"] == 0


def test_polish_appends_rather_than_replaces_the_deterministic_body():
    original = build_answer(goal="g", run={"run_id": "r"}, plan={"goal": "g"},
                            tasks=[task("S01", "implement")],
                            results={"S01": result("S01", summary="done")})
    polished = apply_polish(original, "## 结论\n一切正常。")
    assert polished["polished"] is True
    assert polished["answer_markdown"].startswith("# g\n\n## 结论")
    assert "## 附录：确定性汇总" in polished["answer_markdown"]
    assert original["answer_markdown"] in polished["answer_markdown"]


def test_empty_polish_is_a_no_op():
    original = build_answer(goal="g", run={}, plan={}, tasks=[], results={})
    assert apply_polish(original, "   ") == original


def test_polish_prompt_contains_the_deterministic_digest():
    answer = build_answer(goal="g", run={"run_id": "r"}, plan={"goal": "g"},
                          tasks=[task("S01", "implement")],
                          results={"S01": result("S01", summary="done")})
    prompt = build_polish_prompt(answer)
    assert "不得新增未出现的事实" in prompt
    assert "done" in prompt
