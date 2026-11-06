"""Deterministic verification of task results.

The execution agent never verifies itself. This verifier runs control-plane
checks and can be combined with an independent verification agent's report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import HubConfig
from ..models import (
    Project, ResultPackage, Task, VerificationReport, VerificationStatus,
    files_in_scope, normalize_scope_entries, normalize_scope_path,
    normalize_verification_commands, scope_matches,
)
from ..util import atomic_write_text, ensure_dir, now_iso
from ..workspace import apply_patch, create_worktree, patch_files, remove_worktree
from .paths import WorkspacePaths
from .process_runner import run_command


@dataclass
class VerificationOutcome:
    report: VerificationReport
    patch_path: str = ""
    actual_files: list[str] = field(default_factory=list)
    command_results: list[dict[str, Any]] = field(default_factory=list)
    report_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "report": self.report.to_dict(), "patch_path": self.patch_path,
            "actual_files": self.actual_files, "command_results": self.command_results,
            "report_path": self.report_path,
        }


class Verifier:
    def __init__(self, project: Project, paths: WorkspacePaths, config: HubConfig):
        self.project = project
        self.paths = paths
        self.config = config

    def _scope_severity(self, violations: list[str], out_of_scope: list[str]) -> str:
        """Strictly protect forbidden/high-risk paths; warn on plan gaps."""
        policy = str(getattr(self.config, "scope_violation_policy", "warn")).lower()
        if policy not in ("warn", "warning", "strict"):
            policy = "warn"
        if policy == "strict":
            return "failed"
        risky_tokens = (".env", ".pem", ".key", "id_rsa", "credential", "secret")
        for path in violations:
            if path.startswith("../") or path.startswith("..\\"):
                return "failed"
            if any(scope_matches(pattern, path) for pattern in out_of_scope):
                return "failed"
            if any(token in path.lower() for token in risky_tokens):
                return "failed"
        return "warning"

    def verify(self, task: Task, result: ResultPackage, *,
               upstream_patches: list[str] | None = None) -> VerificationOutcome:
        checks: dict[str, str] = {}
        warnings: list[str] = []
        command_results: list[dict[str, Any]] = []
        patch_path = task.patch_path
        actual_files: list[str] = []

        # 1. Result package completeness is enforced before this call; record it.
        critical_ok = all(
            key in result.payload for key in
            ("summary", "files_changed", "evidence", "tests", "unresolved_items")
        )
        checks["result_schema"] = "passed" if critical_ok else "failed"
        if not critical_ok:
            warnings.append("成果包缺少关键字段")
        if not str(result.payload.get("summary", "")).strip():
            checks["result_schema"] = "failed"
            warnings.append("成果包 summary 为空")
        if not result.payload.get("evidence"):
            if task.spec.writable:
                checks["evidence_check"] = "failed"
                warnings.append("可写任务没有任何可定位证据")
            else:
                checks["evidence_check"] = "warning"
                warnings.append("成果包没有 evidence 条目")
        else:
            checks["evidence_check"] = "passed"

        # 2. Patch presence and scope.
        if task.spec.writable:
            patch_path = patch_path or str(self.paths.patch_path(task.uid))
            actual_files = patch_files(patch_path) if Path(patch_path).exists() else []
            declared = [normalize_scope_path(p) for p in result.files_changed]
            scope = normalize_scope_entries(task.spec.scope)
            out_of_scope = normalize_scope_entries(task.spec.out_of_scope)
            allowed, violations = files_in_scope(actual_files, scope, out_of_scope)
            if violations:
                severity = self._scope_severity(violations, out_of_scope)
                checks["scope_check"] = severity
                label = "修改超出范围" if severity == "failed" else \
                    "修改超出计划范围（已按 warn 策略放行）"
                warnings.append(f"{label}: {violations[:10]}")
            elif not actual_files:
                checks["scope_check"] = "failed"
                warnings.append("可写任务没有任何实际文件变更")
            else:
                checks["scope_check"] = "passed"
            declared_allowed, declared_violations = files_in_scope(
                declared, scope, out_of_scope
            )
            if declared_violations:
                severity = self._scope_severity(declared_violations, out_of_scope)
                checks["declared_scope_check"] = severity
                label = "成果包声明的文件超出范围" if severity == "failed" else \
                    "成果包声明的文件超出计划范围（已按 warn 策略放行）"
                warnings.append(f"{label}: {declared_violations[:10]}")
            else:
                checks["declared_scope_check"] = "passed"
            missing = sorted(set(actual_files) - set(declared))
            extra = sorted(set(declared) - set(actual_files))
            if missing:
                warnings.append(f"成果包未声明实际修改的文件: {missing[:10]}")
            if extra:
                warnings.append(f"成果包声明了未修改的文件: {extra[:10]}")
            checks["files_check"] = "warning" if (missing or extra) else "passed"
        else:
            checks["scope_check"] = "passed"
            checks["files_check"] = "passed"
            if result.files_changed:
                warnings.append("只读任务声明了文件修改，已忽略补丁合并")

        # 3. Patch apply check in an isolated verification worktree.
        verify_dir = self.paths.verify_dir(task.run_id, task.task_id)
        if Path(verify_dir).exists():
            try:
                remove_worktree(self.project.root, verify_dir)
            except Exception:
                pass
        if task.spec.writable and patch_path and Path(patch_path).exists():
            try:
                create_worktree(self.project.root, verify_dir, task.base_commit)
                for upstream in upstream_patches or []:
                    if not Path(upstream).exists() or Path(upstream).stat().st_size == 0:
                        continue
                    up_ok, up_message = apply_patch(
                        verify_dir, upstream, check_only=False, three_way=True)
                    if not up_ok:
                        warnings.append(
                            f"上游补丁无法应用到验证 Worktree: {Path(upstream).name} "
                            f"{up_message[:300]}")
                ok, message = apply_patch(verify_dir, patch_path, check_only=False, three_way=True)
                checks["patch_check"] = "passed" if ok else "failed"
                if not ok:
                    warnings.append(f"Patch 无法应用: {message[:500]}")
            except Exception as exc:
                checks["patch_check"] = "failed"
                warnings.append(f"创建验证 Worktree 失败: {exc}")
        else:
            checks["patch_check"] = "failed" if task.spec.writable else "skipped"
            if task.spec.writable:
                warnings.append("缺少可应用的 Patch")

        # 4. Command verification.
        test_workdir = verify_dir if task.spec.writable and Path(verify_dir).exists() else self.project.root
        commands = normalize_verification_commands(task.spec.verification_commands)
        analysis_command_failed = False
        build_command = self.project.build_command if (
            self.config.verify_build and task.spec.type in ("implementation", "documentation")
        ) else ""
        if build_command and build_command not in commands:
            commands = [build_command] + commands
        server_results: list[bool] = []
        for command in commands:
            proc = run_command(
                command, cwd=test_workdir,
                timeout=self.config.verify_timeout_seconds,
                long_running_timeout=self.config.verify_server_probe_seconds,
            )
            server_ready = bool(proc.long_running and proc.server_ready)
            if proc.long_running:
                server_results.append(server_ready)
            passed = bool(proc.ok or server_ready)
            outcome = {
                "command": command, "exit_code": proc.exit_code, "duration_ms": proc.duration_ms,
                "timed_out": proc.timed_out, "long_running": proc.long_running,
                "server_ready": server_ready,
                "output": (proc.stdout + "\n" + proc.stderr)[-2000:],
                "result": "passed" if passed else "failed",
            }
            command_results.append(outcome)
            if proc.long_running and not server_ready:
                warnings.append(
                    f"常驻命令未在 {self.config.verify_server_probe_seconds}s 内"
                    f"输出监听地址: {command} (exit {proc.exit_code})"
                )
            if not passed:
                if not task.spec.writable:
                    analysis_command_failed = True
                    warnings.append(
                        f"分析任务验证命令失败（按信息性处理，不阻塞任务）: "
                        f"{command} (exit {proc.exit_code})"
                    )
                else:
                    warnings.append(f"验证命令失败: {command} (exit {proc.exit_code})")

        if server_results:
            checks["server_check"] = "passed" if all(server_results) else "failed"
        else:
            checks["server_check"] = "skipped"
        build_results = [r for r in command_results if build_command and r["command"] == build_command]
        task_results = [r for r in command_results if r["command"] != build_command]
        if build_results:
            checks["build_check"] = "failed" if any(r["result"] == "failed" for r in build_results) else "passed"
        else:
            checks["build_check"] = "skipped"
        if task_results:
            failed = any(r["result"] == "failed" for r in task_results)
            if failed and not task.spec.writable:
                checks["unit_tests"] = "warning"
            else:
                checks["unit_tests"] = "failed" if failed else "passed"
        else:
            checks["unit_tests"] = "skipped"
            if task.spec.type in ("implementation", "documentation"):
                warnings.append("任务未声明验证命令，无法自动证明测试通过")

        # 5. Tests claimed vs actually run.
        claimed = result.payload.get("tests") or []
        if claimed:
            claimed_passed = all(
                str(t.get("result", "")).lower() in ("passed", "pass", "ok")
                for t in claimed if isinstance(t, dict)
            )
            actual_failed = any(r["result"] == "failed" for r in command_results)
            if claimed_passed and actual_failed:
                checks["tests_claimed_consistent"] = (
                    "warning" if analysis_command_failed and not task.spec.writable else "failed"
                )
                warnings.append("成果包声称测试通过，但控制平面复跑失败")
            else:
                checks["tests_claimed_consistent"] = "passed"
        else:
            checks["tests_claimed_consistent"] = "skipped"

        # 6. Evidence references exist when they name files.
        missing_evidence = []
        evidence_root = Path(task.workdir) if task.workdir and Path(task.workdir).exists() else Path(self.project.root)
        for item in result.payload.get("evidence") or []:
            if isinstance(item, dict) and item.get("file"):
                path = evidence_root / str(item["file"])
                if not path.exists() and not (Path(self.project.root) / str(item["file"])).exists():
                    missing_evidence.append(str(item["file"]))
        if missing_evidence:
            checks["evidence_files"] = "failed"
            warnings.append(f"证据引用的文件不存在: {missing_evidence[:10]}")
        else:
            checks["evidence_files"] = "passed"

        # 7. Overall status.
        if any(v == "failed" for v in checks.values()):
            status = VerificationStatus.FAILED.value
        elif any(v == "warning" for v in checks.values()) or warnings:
            status = VerificationStatus.WARNING.value
        else:
            status = VerificationStatus.PASSED.value
        report = VerificationReport(
            task_id=task.task_id, verifier_agent="control-plane",
            status=status, checks=checks, warnings=warnings,
            summary=f"控制平面验证：{status}",
            evidence=[{"patch": patch_path, "commands": command_results}],
            created_at=now_iso(), source="control_plane",
        )
        report_path = str(self.paths.report_path(f"verification-{task.uid.replace(':', '_')}.json"))
        ensure_dir(Path(report_path).parent)
        atomic_write_text(report_path, json.dumps({
            "report": report.to_dict(),
            "command_results": command_results,
            "actual_files": actual_files,
        }, ensure_ascii=False, indent=2))
        return VerificationOutcome(
            report=report, patch_path=patch_path, actual_files=actual_files,
            command_results=command_results, report_path=report_path,
        )

    def combine(self, task: Task, deterministic: VerificationReport,
                agent_report: VerificationReport) -> VerificationReport:
        if agent_report.verifier_agent == task.assigned_agent:
            raise ValueError("执行 Agent 不能作为独立验证节点")
        checks = dict(deterministic.checks)
        for key, value in agent_report.checks.items():
            checks[f"agent_{key}"] = value
        if task.spec.read_only:
            # Read-only analysis tasks legitimately have no patch/build/test.
            for key, value in list(checks.items()):
                if key.startswith("agent_") and any(
                        token in key for token in ("build", "test", "patch")) \
                        and value in ("warning", "skipped"):
                    checks[key] = "passed"
        # Verification-side artifacts and pre-merge main-workspace state are
        # not product defects; keep them out of the combined verdict.
        noise_markers = (
            "__pycache__", ".pyc", ".pytest_cache",
            "主工作区仍", "主工作区当前仍", "尚未实际合并", "尚未应用补丁",
            # Verifier reports often annotate expected pre-fix/read-only states.
            "informational", "信息性", "baseline (unpatched)", "pre-fix",
            "by design", "defect remains present", "main workspace remains",
            "unpatched baseline", "before merge", "只读分析任务", "不适用",
        )
        warnings = [
            w for w in (list(deterministic.warnings) + list(agent_report.warnings))
            if not any(marker in w for marker in noise_markers)
        ]
        if any(v == "failed" for v in checks.values()):
            status = VerificationStatus.FAILED.value
        elif any(v == "warning" for v in checks.values()) or warnings:
            status = VerificationStatus.WARNING.value
        else:
            status = VerificationStatus.PASSED.value
        return VerificationReport(
            task_id=task.task_id,
            verifier_agent=f"control-plane+{agent_report.verifier_agent}",
            status=status, checks=checks, warnings=warnings,
            summary=agent_report.summary or deterministic.summary,
            evidence=deterministic.evidence + agent_report.evidence,
            created_at=now_iso(), source="combined",
        )
