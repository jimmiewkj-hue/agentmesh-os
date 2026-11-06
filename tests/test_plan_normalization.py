"""Plan prose scopes/commands must become executable checks."""

from __future__ import annotations

import sys
from pathlib import Path

from agentloop_hub.control.process_runner import run_command
from agentloop_hub.models import (
    TaskSpec, files_in_scope, normalize_scope_entries,
    normalize_verification_commands, scope_matches,
)


def test_prose_scope_becomes_assets_glob():
    scope = normalize_scope_entries([
        "新增 qingming-3d/src/scene/assets/**（materials.js、bridge.js、boats.js）",
        "仅允许创建与修改上述 assets 目录内文件",
    ])
    assert "qingming-3d/src/scene/assets/**" in scope
    allowed, violations = files_in_scope(
        ["qingming-3d/src/scene/assets/boats.js",
         "qingming-3d/src/scene/assets/index.js"],
        scope, [],
    )
    assert not violations
    assert len(allowed) == 2


def test_bare_file_scope_matches_any_directory():
    assert scope_matches("package-lock.json", "qingming-3d/package-lock.json")
    assert scope_matches("README.md", "qingming-3d/README.md")


def test_prose_sibling_directory_gets_project_prefix():
    scope = normalize_scope_entries([
        "修改 qingming-3d/vite.config.js、scripts/**、dist/** 与 package-lock.json",
    ])
    assert "qingming-3d/vite.config.js" in scope
    assert "qingming-3d/scripts/**" in scope
    assert "qingming-3d/dist/**" in scope


def test_planning_paths_and_absolute_git_are_rewritten():
    commands = normalize_verification_commands([
        r"git -C D:\temp\qingmingshanghetu status --short",
        r"Test-Path D:\temp\qingmingshanghetu\.agentloop\planning\plan_x\workspace\qingming-3d\dist",
        r"Get-ChildItem D:\temp\qingmingshanghetu\.agentloop\planning\plan_x\workspace\qingming-3d\src",
        "node -v",
    ])
    assert commands[0] == "git status --short"
    assert "planning" not in commands[1]
    assert "qingming-3d\\dist" in commands[1] or "qingming-3d/dist" in commands[1]
    assert "planning" not in commands[2]
    assert commands[3] == "node -v"


def test_writable_taskspec_rejects_unparseable_scope():
    spec = TaskSpec(task_id="T1", title="x", objective="x", type="implementation",
                    scope=["只允许修改上面提到的那些文件"], token_budget=1000,
                    time_limit_minutes=5)
    try:
        spec.validate()
    except Exception as exc:
        assert "scope" in str(exc)
    else:
        raise AssertionError("prose-only writable scope should be rejected")


def test_writable_taskspec_normalizes_prose_scope():
    spec = TaskSpec(task_id="T1", title="x", objective="x", type="implementation",
                    scope=["新增 qingming-3d/src/scene/assets/**"], token_budget=1000,
                    time_limit_minutes=5)
    spec.validate()
    assert spec.scope == ["qingming-3d/src/scene/assets/**"]


def test_powershell_commands_run_on_windows(tmp_path):
    if sys.platform != "win32":
        return
    result = run_command("Test-Path .", cwd=str(tmp_path), timeout=20)
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip().lower() == "true"


def test_opencode_structured_error_is_not_lost():
    from agentloop_hub.adapters.opencode import OpenCodeAdapter

    stdout = (
        '{"type":"step_start"}\n'
        '{"type":"error","error":{"name":"APIError","data":'
        '{"message":"This model is not available in your country","statusCode":403}}}\n'
    )
    assert "not available in your country" in OpenCodeAdapter.error_summary(stdout, "")


def test_scope_violation_policy_warns_or_fails():
    from types import SimpleNamespace
    from agentloop_hub.control.verification import Verifier

    verifier = Verifier(None, None, SimpleNamespace(scope_violation_policy="warn"))
    assert verifier._scope_severity(["src/main.js"], []) == "warning"
    assert verifier._scope_severity(["../outside.js"], []) == "failed"
    assert verifier._scope_severity([".env"], []) == "failed"
    # warn: an explicit out_of_scope edit is reported but does not kill the
    # task; only strict blocks it (plus traversal/secrets in every policy).
    assert verifier._scope_severity(["src/main.js"], ["src/main.js"]) == "warning"
    strict = Verifier(None, None, SimpleNamespace(scope_violation_policy="strict"))
    assert strict._scope_severity(["src/main.js"], []) == "failed"
    assert strict._scope_severity(["src/main.js"], ["src/main.js"]) == "failed"
