"""Deterministic segmentation: attributes in, a small task graph out."""

from __future__ import annotations

import pytest

from agentloop_hub.control.roles import RolePolicy
from agentloop_hub.control.segment import Segmenter


def seg(goal: str, *, test_command: str = "", max_tasks: int = 4):
    return Segmenter(RolePolicy.default(), max_tasks=max_tasks,
                     test_command=test_command).segment(goal)


def roles(segment) -> list[str]:
    return [task.role for task in segment.tasks]


def test_a_named_file_fix_produces_one_write_task_and_one_verification():
    segment = seg("修复 calc.py 的加法错误并补充测试", test_command="python -m pytest -q")
    assert roles(segment) == ["implement", "verify"]
    implement = segment.tasks[0]
    assert implement.scope == ["calc.py"]
    assert implement.type == "implementation"
    assert segment.tasks[1].verification_commands == ["python -m pytest -q"]
    assert segment.tasks[1].dependencies == [implement.task_id]


def test_design_plus_implementation_starts_with_a_plan_and_uses_the_commander():
    segment = seg("设计一个缓存层并实现", test_command="python -m pytest -q")
    assert roles(segment) == ["plan", "implement", "verify"]
    assert segment.tasks[0].type == "architecture"
    assert segment.tasks[1].scope == ["**"]  # no file named -> explicit whole-repo scope


def test_a_question_never_becomes_a_write_task():
    segment = seg("为什么 merge.py 的策略要按 patch 优先？")
    assert roles(segment) == ["explore"]
    assert segment.tasks[0].scope == []
    assert segment.signals["question"] is True


def test_pure_review_is_a_single_read_only_task():
    segment = seg("审阅一下 agentloop_hub/control 的安全性")
    assert roles(segment) == ["review"]
    assert segment.tasks[0].scope == []
    # a directory hint becomes a directory scope, not the whole repo
    assert "agentloop_hub/control/**" in segment.signals["paths"]


def test_documentation_request_does_not_touch_code():
    segment = seg("写一份使用说明文档")
    assert roles(segment) == ["write"]
    assert segment.tasks[0].type == "documentation"


def test_scan_request_stays_read_only_and_cheap():
    segment = seg("扫描整个项目看看有哪些模块")
    assert roles(segment) == ["explore"]
    assert segment.tasks[0].token_budget <= 20000


def test_an_unclassifiable_request_falls_back_to_explore_with_low_confidence():
    segment = seg("嗯，那个东西你看着办")
    assert roles(segment) == ["explore"]
    assert segment.confidence < 0.5


def test_task_count_is_capped():
    segment = seg("设计并实现并审阅并写文档并验证 calc.py", max_tasks=3)
    assert len(segment.tasks) <= 3
    assert any("收敛到 3" in note for note in segment.notes)


def test_absolute_paths_are_not_used_as_scope():
    segment = seg(r"把 D:/qoder/secret.py 改一下")
    assert all(not p.startswith("J:") for p in segment.signals["paths"])
    write = next(t for t in segment.tasks if t.writable)
    assert write.scope == ["**"]


def test_write_task_without_a_named_file_is_flagged_medium_risk():
    segment = seg("重构一下登录逻辑")
    write = next(t for t in segment.tasks if t.writable)
    assert write.risk_level == "medium"
    assert write.scope == ["**"]


def test_every_task_carries_a_role_known_to_the_policy():
    policy = RolePolicy.default()
    segment = Segmenter(policy, test_command="pytest").segment(
        "设计并实现缓存层，补测试，写文档")
    assert segment.tasks
    for task in segment.tasks:
        assert task.role in policy.roles
        assert task.type == policy.roles[task.role].task_type
        assert task.role  # persisted per task, not inferred at run time


def test_task_ids_are_sequential_and_dependencies_form_a_chain():
    segment = seg("设计并实现缓存层，补测试", test_command="pytest")
    ids = [t.task_id for t in segment.tasks]
    assert ids == [f"S{index:02d}" for index in range(1, len(ids) + 1)]
    for previous, current in zip(segment.tasks, segment.tasks[1:]):
        assert current.dependencies == [previous.task_id]


def test_adding_a_function_is_a_write_task_not_a_scan():
    """Regression: a real run classified 增加 as a read-only scan and wrote nothing."""
    segment = seg("给 calc.py 增加 multiply 函数")
    assert roles(segment) == ["implement"]
    assert segment.tasks[0].writable
    assert segment.tasks[0].scope == ["calc.py"]


def test_polite_question_form_still_becomes_work_when_it_has_an_imperative():
    segment = seg("能不能给 calc.py 增加一个 multiply 函数？")
    assert roles(segment) == ["implement"]
    assert segment.signals["question"] is False


def test_empty_goal_is_rejected():
    with pytest.raises(ValueError):
        seg("   ")
