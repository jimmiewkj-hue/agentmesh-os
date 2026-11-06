"""Deterministic verification of task results.

The execution agent never verifies itself. This verifier runs control-plane
checks and can be combined with an independent verification agent's report.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
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
from ..workspace import (
    PATCH_EXCLUDE_PREFIXES, apply_patch, create_worktree, patch_files, remove_worktree,
)
from .paths import WorkspacePaths
from .process_runner import run_command


@dataclass
class VerificationOutcome:
    report: VerificationReport
    patch_path: str = ""
    actual_files: list[str] = field(default_factory=list)
    command_results: list[dict[str, Any]] = field(default_factory=list)
    report_path: str = ""
    snapshot_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "report": self.report.to_dict(), "patch_path": self.patch_path,
            "actual_files": self.actual_files, "command_results": self.command_results,
            "report_path": self.report_path, "snapshot_dir": self.snapshot_dir,
        }


class Verifier:
    def __init__(self, project: Project, paths: WorkspacePaths, config: HubConfig):
        self.project = project
        self.paths = paths
        self.config = config

    def _scope_severity(self, violations: list[str], out_of_scope: list[str]) -> str:
        """Strictly protect forbidden/high-risk paths; warn on plan gaps.

        In the default ``warn`` policy an explicit out-of-scope path is
        reported to the merge approval but does not kill an otherwise valid
        task; ``strict`` blocks any out-of-scope change.  Path traversal and
        secret-bearing files always block.
        """
        policy = str(getattr(self.config, "scope_violation_policy", "warn")).lower()
        if policy not in ("warn", "warning", "strict"):
            policy = "warn"
        if policy == "strict":
            return "failed"
        risky_tokens = (".env", ".pem", ".key", "id_rsa", "credential", "secret")
        for path in violations:
            if path.startswith("../") or path.startswith("..\\"):
                return "failed"
            if any(token in path.lower() for token in risky_tokens):
                return "failed"
        return "warning"

    @staticmethod
    def _command_env(command: str) -> dict[str, str]:
        """Locate Blender when the planner assumed it was on PATH."""
        if "blender" not in str(command).lower() or shutil.which("blender"):
            return {}
        base = Path("C:/Program Files/Blender Foundation")
        candidates = sorted(base.glob("Blender*/blender.exe")) if base.exists() else []
        if not candidates:
            return {}
        return {"PATH": str(candidates[-1].parent) + os.pathsep
                + os.environ.get("PATH", "")}

    def _seed_js_dependencies(self, task: Task, verify_dir: Path,
                              warnings: list[str]) -> None:
        """Reuse installed node_modules when the frozen lockfile is identical.

        Verification runs in a fresh worktree built from patches; dependency
        directories are deliberately excluded from patches.  A later node that
        only runs ``npm run build`` would otherwise fail with "vite: not
        found" even though the documented setup is identical to the executor's.
        """
        verify_dir = Path(verify_dir)
        if not (verify_dir / "package.json").exists():
            return
        if (verify_dir / "node_modules").exists():
            return
        source = Path(task.workdir or "")
        if not (source / "node_modules").exists():
            return
        src_pkg = source / "package.json"
        dst_pkg = verify_dir / "package.json"
        if src_pkg.exists() and dst_pkg.exists():
            if hashlib.sha256(src_pkg.read_bytes()).hexdigest() != \
                    hashlib.sha256(dst_pkg.read_bytes()).hexdigest():
                warnings.append("package.json 与执行工作树不一致，未复用 node_modules")
                return
        src_lock = source / "package-lock.json"
        dst_lock = verify_dir / "package-lock.json"
        if src_lock.exists() and dst_lock.exists():
            src_hash = hashlib.sha256(src_lock.read_bytes()).hexdigest()
            dst_hash = hashlib.sha256(dst_lock.read_bytes()).hexdigest()
            if src_hash != dst_hash:
                warnings.append(
                    "依赖锁文件与冻结补丁不一致；package.json 相同，仍复用已安装依赖")
        try:
            shutil.copytree(source / "node_modules", verify_dir / "node_modules")
            warnings.append("验证工作树复用执行工作树的 node_modules")
        except Exception as exc:  # dependency seeding is an optimization
            warnings.append(f"复用 node_modules 失败: {str(exc)[:200]}")

    def _excluded_artifact(self, path: str) -> bool:
        """Build outputs, dependencies and harness files are not source edits."""
        rel = normalize_scope_path(path)
        if not rel:
            return True
        if rel == ".agentloop-task" or rel.startswith(".agentloop-task/") \
                or rel == ".agentmesh" or rel.startswith(".agentmesh/"):
            return True
        for raw in (self.config.excluded_paths or []):
            pattern = normalize_scope_path(raw).rstrip("/")
            if not pattern:
                continue
            if rel == pattern or rel.startswith(pattern + "/"):
                return True
            if scope_matches(raw, rel):
                return True
        return False

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
            raw_actual = patch_files(patch_path) if Path(patch_path).exists() else []
            actual_files = [p for p in raw_actual if not self._excluded_artifact(p)]
            declared = [normalize_scope_path(p) for p in result.files_changed
                        if not self._excluded_artifact(p)]
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
                upstream_ok = True
                for upstream in upstream_patches or []:
                    up_path = Path(upstream)
                    if not up_path.exists() or up_path.stat().st_size == 0:
                        checks["upstream_patch_check"] = "failed"
                        warnings.append(
                            f"缺少必需的上游补丁，验证已中止: {upstream}")
                        upstream_ok = False
                        break
                    up_ok, up_message = apply_patch(
                        verify_dir, upstream, check_only=False, three_way=True,
                        exclude_prefixes=PATCH_EXCLUDE_PREFIXES)
                    if not up_ok:
                        checks["upstream_patch_check"] = "failed"
                        warnings.append(
                            f"上游补丁无法应用到验证 Worktree: {up_path.name} "
                            f"{up_message[:300]}")
                        upstream_ok = False
                        break
                if "upstream_patch_check" not in checks:
                    checks["upstream_patch_check"] = "passed"
                if upstream_ok:
                    ok, message = apply_patch(
                        verify_dir, patch_path, check_only=False, three_way=True,
                        exclude_prefixes=PATCH_EXCLUDE_PREFIXES)
                    checks["patch_check"] = "passed" if ok else "failed"
                    if not ok:
                        warnings.append(f"Patch 无法应用: {message[:500]}")
                else:
                    checks["patch_check"] = "failed"
            except Exception as exc:
                checks["patch_check"] = "failed"
                warnings.append(f"创建验证 Worktree 失败: {exc}")
        else:
            checks["patch_check"] = "failed" if task.spec.writable else "skipped"
            if task.spec.writable:
                warnings.append("缺少可应用的 Patch")

        # 4. Command verification.  Commands always run inside a candidate
        # snapshot: the task's isolated worktree, or a fresh worktree of the
        # task base commit.  The user's main workspace is never used.
        test_workdir = ""
        if task.spec.writable:
            if Path(verify_dir).exists() and checks.get("patch_check") == "passed":
                test_workdir = str(verify_dir)
            else:
                warnings.append("Patch 未成功装载，验证命令未执行")
        else:
            same_as_user_tree = bool(
                task.workdir
                and Path(task.workdir).exists()
                and Path(task.workdir).resolve() == Path(self.project.root).resolve())
            if task.workdir and Path(task.workdir).exists() and not same_as_user_tree:
                test_workdir = str(task.workdir)
            else:
                try:
                    create_worktree(self.project.root, verify_dir, task.base_commit)
                    test_workdir = str(verify_dir)
                except Exception as exc:
                    warnings.append(f"无法建立只读任务的输入快照: {exc}")
        checks["snapshot_check"] = "passed" if test_workdir else "failed"
        if task.spec.writable and test_workdir and task.workdir \
                and Path(test_workdir).resolve() != Path(task.workdir).resolve():
            self._seed_js_dependencies(task, Path(test_workdir), warnings)

        required_commands = bool(
            task.spec.writable
            or task.spec.type in ("verification", "review")
            or getattr(task.spec, "verification_required", False)
        )
        commands = normalize_verification_commands(task.spec.verification_commands) \
            if test_workdir else []
        informational_command_failed = False
        build_command = self.project.build_command if (
            self.config.verify_build and task.spec.type in ("implementation", "documentation")
        ) else ""
        if build_command and build_command not in commands:
            commands = [build_command] + commands
        if task.spec.writable and test_workdir:
            work = Path(test_workdir)
            if (work / "package.json").exists() and not (work / "node_modules").exists():
                bootstrap = "npm install --no-audit --no-fund"
                proc = run_command(
                    bootstrap, cwd=test_workdir,
                    timeout=min(600, int(self.config.verify_timeout_seconds)))
                command_results.append({
                    "command": bootstrap, "exit_code": proc.exit_code,
                    "duration_ms": proc.duration_ms, "timed_out": proc.timed_out,
                    "long_running": False, "server_ready": False,
                    "output": (proc.stdout + "\n" + proc.stderr)[-2000:],
                    "result": "passed" if proc.ok else "failed",
                    "bootstrap": True,
                })
                if not proc.ok:
                    warnings.append(
                        f"验证依赖安装失败: {bootstrap} (exit {proc.exit_code})")
        server_results: list[bool] = []
        for command in commands:
            proc = run_command(
                command, cwd=test_workdir,
                timeout=self.config.verify_timeout_seconds,
                long_running_timeout=self.config.verify_server_probe_seconds,
                env=self._command_env(command),
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
                if not required_commands:
                    informational_command_failed = True
                    warnings.append(
                        f"分析任务信息性命令失败（记录为发现，不作为验收证据）: "
                        f"{command} (exit {proc.exit_code})"
                    )
                else:
                    warnings.append(f"必需验证命令失败: {command} (exit {proc.exit_code})")

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
            if failed and not required_commands:
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
                    "warning" if informational_command_failed and not required_commands
                    else "failed"
                )
                warnings.append("成果包声称测试通过，但控制平面复跑失败")
            else:
                checks["tests_claimed_consistent"] = "passed"
        else:
            checks["tests_claimed_consistent"] = "skipped"

        # 6. Evidence references exist when they name files.
        missing_evidence = []
        evidence_root = Path(test_workdir) if test_workdir else (
            Path(task.workdir) if task.workdir else Path(self.project.root))
        for item in result.payload.get("evidence") or []:
            if isinstance(item, dict) and item.get("file"):
                path = evidence_root / str(item["file"])
                if not path.exists():
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
            snapshot_dir=test_workdir,
        )

    def combine(self, task: Task, deterministic: VerificationReport,
                agent_report: VerificationReport) -> VerificationReport:
        if agent_report.verifier_agent == task.assigned_agent:
            raise ValueError("执行 Agent 不能作为独立验证节点")
        checks = dict(deterministic.checks)
        for key, value in agent_report.checks.items():
            checks[f"agent_{key}"] = value
        # A report's own top-level failure is a fact, even when its checks
        # object is empty.  It must never be washed into "passed".
        if str(agent_report.status).lower() == VerificationStatus.FAILED.value:
            checks["agent_report_status"] = "failed"
        if task.spec.read_only:
            # Read-only analysis tasks legitimately skip patch/build/test.
            for key, value in list(checks.items()):
                if key.startswith("agent_") and any(
                        token in key for token in ("build", "test", "patch")) \
                        and value == "skipped":
                    checks[key] = "passed"
        # Only harness-artifact noise is filtered.  Natural-language warnings
        # such as "defect remains present" are evidence and stay in the report.
        noise_markers = ("__pycache__", ".pyc", ".pytest_cache")
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
