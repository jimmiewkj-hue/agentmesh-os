from __future__ import annotations

import pytest

from agentloop_hub.errors import StateTransitionError, ValidationError
from agentloop_hub.models import (
    ResultPackage, TaskPlan, TaskSpec, TaskStatus, VerificationReport,
    assert_task_transition, files_in_scope, pattern_overlap, scope_matches,
    RESULT_REQUIRED_FIELDS,
)


def test_task_spec_requires_scope_for_writable_task():
    spec = TaskSpec(task_id="T1", title="x", objective="y", type="implementation")
    with pytest.raises(ValidationError):
        spec.validate()
    spec.scope = ["src/**"]
    spec.validate()
    assert spec.writable


def test_analysis_task_rejects_test_verification_commands():
    spec = TaskSpec(
        task_id="T1", title="scan", objective="read", type="analysis",
        verification_commands=["python -m unittest -q"],
    )
    with pytest.raises(ValidationError) as exc:
        spec.validate()
    assert "analysis 任务不能携带测试类" in str(exc.value)

    ok = TaskSpec(
        task_id="T1", title="scan", objective="read", type="analysis",
        verification_commands=["git status --short"],
    )
    ok.validate()

    impl = TaskSpec(
        task_id="T2", title="fix", objective="fix", type="implementation",
        scope=["calc.py"], verification_commands=["python -m unittest -q"],
    )
    impl.validate()


def test_task_spec_rejects_self_dependency():
    spec = TaskSpec(task_id="T1", title="x", objective="y", type="implementation",
                    scope=["a.py"], dependencies=["T1"])
    with pytest.raises(ValidationError):
        spec.validate()


def test_plan_rejects_cycle_and_missing_dependency():
    t1 = TaskSpec(task_id="T1", title="a", objective="a", dependencies=["T2"])
    t2 = TaskSpec(task_id="T2", title="b", objective="b", dependencies=["T1"])
    plan = TaskPlan(plan_id="p", project_id="prj", goal="g", tasks=[t1, t2])
    with pytest.raises(ValidationError):
        plan.validate()

    t3 = TaskSpec(task_id="T3", title="c", objective="c", dependencies=["T9"])
    plan2 = TaskPlan(plan_id="p2", project_id="prj", goal="g", tasks=[t3])
    with pytest.raises(ValidationError):
        plan2.validate()


def test_plan_unknown_field_rejected():
    with pytest.raises(ValidationError):
        TaskPlan.from_dict({
            "goal": "g", "tasks": [{
                "task_id": "T1", "title": "t", "objective": "o", "type": "analysis",
                "unknown": 1,
            }],
        }, project_id="p")


def test_plan_structured_requirement_fields():
    plan = TaskPlan.from_dict({
        "goal": "g",
        "requirements": ["r1"],
        "nonfunctional": ["n1"],
        "risks": ["risk"],
        "open_questions": ["q"],
        "tasks": [{"task_id": "T1", "title": "t", "objective": "o", "type": "analysis"}],
    }, project_id="p")
    assert plan.requirements == ["r1"]
    assert plan.nonfunctional == ["n1"]
    assert plan.risks == ["risk"]
    assert plan.open_questions == ["q"]
    assert plan.to_dict()["requirements"] == ["r1"]


def test_topological_order():
    t1 = TaskSpec(task_id="A", title="a", objective="a")
    t2 = TaskSpec(task_id="B", title="b", objective="b")
    t3 = TaskSpec(task_id="C", title="c", objective="c", dependencies=["A", "B"])
    plan = TaskPlan(plan_id="p", project_id="prj", goal="g", tasks=[t3, t1, t2])
    assert [t.task_id for t in plan.topological_order()] == ["A", "B", "C"]


def test_scope_matching_patterns():
    assert scope_matches("src/**", "src/auth/token.ts")
    assert scope_matches("src/auth/*.ts", "src/auth/token.ts")
    assert not scope_matches("src/auth/*.ts", "src/auth/deep/token.ts")
    assert scope_matches("calc.py", "calc.py")
    assert scope_matches("src/", "src/auth/token.ts")
    assert not scope_matches("src/auth/**", "tests/auth/test.ts")
    assert scope_matches("**/package-lock.json", "a/b/package-lock.json")


def test_files_in_scope():
    allowed, violations = files_in_scope(
        ["src/a.py", "tests/a.py", "db/schema.sql"],
        ["src/**", "tests/**"], ["db/**"],
    )
    assert allowed == ["src/a.py", "tests/a.py"]
    assert violations == ["db/schema.sql"]


def test_pattern_overlap():
    assert pattern_overlap(["src/**"], ["src/auth/**"]) == ["src/**"]
    assert pattern_overlap(["src/**"], ["tests/**"]) == []
    assert pattern_overlap(["**"], ["**/package-lock.json"]) == ["**"]


def test_result_package_normalizes_common_model_deviations():
    payload = {key: [] for key in RESULT_REQUIRED_FIELDS}
    payload.update({
        "task_id": "T1", "agent_id": "a", "status": "completed", "summary": "done",
        "token_usage": 0, "cost_estimate": 0,
        "recommended_next_action": "继续修复",
    })
    normalized, changed = ResultPackage.normalize(payload)
    assert "token_usage" in changed
    assert "cost_estimate" in changed
    assert "recommended_next_action" in changed
    assert normalized["token_usage"] == {"total": 0}
    assert normalized["cost_estimate"]["amount"] == 0
    assert normalized["recommended_next_action"] == ["继续修复"]
    package = ResultPackage.validate(normalized, expected_task_id="T1",
                                     expected_agent_id="a")
    assert package.summary == "done"
    assert package.token_usage == {"total": 0}


def test_result_package_requires_all_fields():
    payload = {key: [] for key in RESULT_REQUIRED_FIELDS}
    payload.update({"task_id": "T1", "agent_id": "a", "status": "completed",
                    "summary": "ok", "token_usage": {}, "cost_estimate": {}})
    ResultPackage.validate(payload, expected_task_id="T1", expected_agent_id="a")
    bad = dict(payload)
    bad.pop("unresolved_items")
    with pytest.raises(ValidationError):
        ResultPackage.validate(bad, expected_task_id="T1", expected_agent_id="a")


def test_result_package_rejects_wrong_agent_and_unknown_field():
    payload = {key: [] for key in RESULT_REQUIRED_FIELDS}
    payload.update({"task_id": "T1", "agent_id": "a", "status": "completed",
                    "summary": "ok", "token_usage": {}, "cost_estimate": {}})
    with pytest.raises(ValidationError):
        ResultPackage.validate(payload, expected_agent_id="b")
    payload["extra"] = 1
    with pytest.raises(ValidationError):
        ResultPackage.validate(payload)


def test_verification_report_executor_cannot_self_verify():
    report = {
        "task_id": "T1", "verifier_agent": "a", "verification_status": "passed",
        "checks": {"scope_check": "passed"}, "warnings": [],
    }
    with pytest.raises(ValidationError):
        VerificationReport.validate(report, expected_task_id="T1", executor_agent="a")
    VerificationReport.validate(report, expected_task_id="T1", executor_agent="b")


def test_task_state_machine_rejects_invalid_transition():
    assert_task_transition(TaskStatus.QUEUED.value, TaskStatus.RUNNING.value)
    with pytest.raises(StateTransitionError):
        assert_task_transition(TaskStatus.COMPLETED.value, TaskStatus.RUNNING.value)
    with pytest.raises(StateTransitionError):
        assert_task_transition(TaskStatus.RUNNING.value, TaskStatus.QUEUED.value)
