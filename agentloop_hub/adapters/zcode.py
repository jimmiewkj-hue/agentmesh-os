"""ZCode headless CLI adapter (verified against ZCode 3.17.2 / zcode CLI 0.17.1).

Reverse-engineered trigger surface (all verified on this machine, see
docs/ZCode自动化触发.md):

  * ZCode Desktop ships a real CLI core at ``resources\\glm\\zcode.cjs``.
    Running it through the Electron binary in Node mode works without the
    desktop app:  ``ELECTRON_RUN_AS_NODE=1 ZCode.exe zcode.cjs -p "<task>"``
  * ``-p`` runs one prompt headlessly (permission mode defaults to ``yolo``),
    ``--cwd`` pins the workspace, ``--json`` prints one JSON object with
    ``sessionId`` / ``response`` / ``usage``, ``--resume <sess_…>`` continues a
    persisted session.
  * Model selection: the headless CLI picks the first available model of the
    merged provider registry (``providerOrder[0]`` first).  Both
    ``ZCODE_BUILTIN_PROVIDER_CONFIG_FILE`` and ``ZCODE_PERSONAL_PROVIDER_CONFIG_FILE``
    must be provided together; pointing the personal one at a temporary copy
    with the target provider first in ``providerOrder`` and the target model
    first in ``personalModelIds``/``modelOrder`` pins provider AND model.
    The user's real ``provider_config.json`` is never modified.
  * ``ZCODE_MODEL_RETRY_MAX_RETRIES`` caps the model retry loop; without it a
    provider business error (e.g. expired subscription) retries 11 times with
    exponential backoff before failing.
  * Readback: ``~/.zcode/cli/db/db.sqlite`` tables ``session`` / ``model_usage``
    plus ``~/.zcode/cli/rollout/model-io-sess_*.jsonl``.  Both are read-only.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from ..models import AgentRecord
from ..util import atomic_write_text, ensure_dir, read_text
from .base import ExecutionOutcome, ExecutionRequest, ExecutionUsage
from .cli import CliAgentAdapter, _load_json_file
from .process import run_process

DEFAULT_EXECUTABLE_CANDIDATES = (
    "C:/Program Files/ZCode/ZCode.exe",
    "C:/Program Files (x86)/ZCode/ZCode.exe",
)
CLI_RELATIVE_PATH = ("resources", "glm", "zcode.cjs")
DB_RELATIVE_PATH = (".zcode", "cli", "db", "db.sqlite")
PROVIDER_CONFIG_RELATIVE_PATH = (".zcode", "v2", "provider_config.json")


def resolve_zcode_install(config: dict[str, Any] | None = None) -> tuple[str, str]:
    """Return ``(zcode_exe, zcode_cjs)``; either may be empty when missing."""
    cfg = dict(config or {})
    exe = str(cfg.get("zcode_exe") or cfg.get("executable") or "")
    if exe and not Path(exe).exists():
        exe = ""
    if not exe:
        local = Path(os.environ.get("LOCALAPPDATA") or "") / "Programs" / "ZCode" / "ZCode.exe"
        for candidate in (local, *[Path(p) for p in DEFAULT_EXECUTABLE_CANDIDATES]):
            if candidate.exists():
                exe = str(candidate)
                break
    cjs = str(cfg.get("zcode_cjs") or "")
    if cjs and not Path(cjs).exists():
        cjs = ""
    if not cjs and exe:
        candidate = Path(exe).parent.joinpath(*CLI_RELATIVE_PATH)
        if candidate.exists():
            cjs = str(candidate)
    return exe, cjs


def resolve_provider_config_path(config: dict[str, Any] | None = None) -> str:
    cfg = dict(config or {})
    override = str(cfg.get("provider_config") or "")
    if override and Path(override).exists():
        return override
    candidate = Path.home().joinpath(*PROVIDER_CONFIG_RELATIVE_PATH)
    return str(candidate) if candidate.exists() else ""


def resolve_builtin_provider_config(config: dict[str, Any] | None = None) -> str:
    """Locate the app's own materialized builtin provider config (read-only)."""
    cfg = dict(config or {})
    override = str(cfg.get("builtin_provider_config") or "")
    if override and Path(override).exists():
        return override
    env = os.environ.get("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE", "").strip()
    if env and Path(env).exists():
        return env
    candidates: list[Path] = []
    runtime_root = Path.home() / ".zcode" / "v2" / "runtime" / "provider"
    if runtime_root.exists():
        candidates.extend(sorted(runtime_root.glob("*/*/endpoint-*/zcode-builtin.json"),
                                 key=lambda p: p.stat().st_mtime, reverse=True))
    exe, _ = resolve_zcode_install(cfg)
    if exe:
        candidates.append(Path(exe).parent / "resources" / "config" / "provider" / "zcode-builtin.json")
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def _provider_model_candidates(provider_config: dict[str, Any]) -> tuple[list[str], dict[str, list[str]]]:
    """Real provider ids and their model lists from the provider config."""
    config = provider_config.get("config") if isinstance(provider_config, dict) else None
    if not isinstance(config, dict):
        return [], {}
    order = [str(p) for p in (config.get("providerOrder") or [])]
    candidates: dict[str, list[str]] = {}
    rules = ((config.get("providerConfigRules") or {}).get("providerRules")) or []
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            provider_id = str(rule.get("providerId") or "")
            rule_config = rule.get("config") if isinstance(rule.get("config"), dict) else {}
            models: list[str] = []
            for key in ("personalModelIds", "modelOrder"):
                values = rule_config.get(key)
                if isinstance(values, list):
                    models.extend(str(v) for v in values)
            if models:
                candidates.setdefault(provider_id, [])
                for model_id in models:
                    if model_id not in candidates[provider_id]:
                        candidates[provider_id].append(model_id)
    model_rules = ((config.get("modelConfigRules") or {}).get("providerModelRules")) or []
    if isinstance(model_rules, list):
        for rule in model_rules:
            if not isinstance(rule, dict):
                continue
            provider_id = str(rule.get("providerId") or "")
            model_id = str(rule.get("modelId") or "")
            rule_cfg = rule.get("config") if isinstance(rule.get("config"), dict) else {}
            if rule_cfg.get("enabled") is False:
                continue
            bucket = candidates.setdefault(provider_id, [])
            if model_id and model_id not in bucket:
                bucket.append(model_id)
    return order, candidates


def resolve_model_target(model: str, provider_config: dict[str, Any]) -> tuple[str, str] | None:
    """Resolve a bare or ``provider/model`` id against the provider config.

    Bare model ids resolve to the provider that offers them and appears first
    in ``providerOrder``.  Slash ids are validated against the real config:
    a prefix that is not a known provider (e.g. the catalog display prefix
    ``z-ai`` in ``z-ai/glm-5.2``) must not be pinned as if it existed — the
    model part is searched across the real providers instead (2026-11-07
    real-chain defect #4).  Returns ``None`` when nothing matches.
    """
    target = str(model or "").strip()
    if not target:
        return None
    order, candidates = _provider_model_candidates(provider_config)
    known_providers = set(candidates) | {str(p) for p in order}
    rules = (((provider_config.get("config") or {}).get("providerConfigRules") or {})
             .get("providerRules")) or []
    if isinstance(rules, list):
        known_providers |= {str(r.get("providerId") or "") for r in rules
                            if isinstance(r, dict)}
    known_providers.discard("")

    def _ranked(model_id: str) -> tuple[str, str] | None:
        ranked: list[tuple[int, int, str, str]] = []
        for provider_id, models in candidates.items():
            if model_id not in models:
                continue
            rank = order.index(provider_id) if provider_id in order else len(order)
            ranked.append((rank, models.index(model_id), provider_id, model_id))
        if not ranked:
            return None
        ranked.sort()
        return ranked[0][2], ranked[0][3]

    if "/" in target:
        # The full id itself may be a real model id (e.g. "/deepseek-v4-pro"
        # offered by provider "deepseek" renders as "deepseek//deepseek-v4-pro").
        full = _ranked(target)
        if full:
            return full
        provider, _, model_id = target.partition("/")
        provider, model_id = provider.strip(), model_id.strip()
        if provider and model_id:
            if provider in known_providers:
                return provider, model_id
            # Unknown prefix: search the model part across real providers.
            suffix = _ranked(model_id)
            if suffix:
                return suffix
        return None
    return _ranked(target)


def build_pinned_provider_config(source: dict[str, Any], provider_id: str,
                                 model_id: str) -> dict[str, Any]:
    """Copy the provider config and pin provider+model for one headless run.

    The copy is what ``ZCODE_PERSONAL_PROVIDER_CONFIG_FILE`` points at; the
    source (the user's real ``provider_config.json``) is never written.
    """
    data = copy.deepcopy(source)
    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    if not isinstance(config, dict):
        config = {}
        data["config"] = config
    order = [str(p) for p in (config.get("providerOrder") or [])]
    order = [provider_id] + [p for p in order if p != provider_id]
    config["providerOrder"] = order

    rules = ((config.get("providerConfigRules") or {}).get("providerRules")) or []
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict) or str(rule.get("providerId") or "") != provider_id:
                continue
            rule_config = rule.get("config") if isinstance(rule.get("config"), dict) else {}
            rest: list[str] = []
            for key in ("personalModelIds", "modelOrder"):
                values = rule_config.get(key)
                if isinstance(values, list):
                    rest.extend(str(v) for v in values)
            rest = [m for m in rest if m and m != model_id]
            pinned = [model_id] + rest
            rule_config["personalModelIds"] = pinned
            rule_config["modelOrder"] = pinned
            rule["config"] = rule_config

    model_rules = config.get("modelConfigRules")
    if not isinstance(model_rules, dict):
        model_rules = {}
        config["modelConfigRules"] = model_rules
    provider_model_rules = model_rules.get("providerModelRules")
    if not isinstance(provider_model_rules, list):
        provider_model_rules = []
        model_rules["providerModelRules"] = provider_model_rules
    seen = False
    for rule in provider_model_rules:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("providerId") or "") != provider_id:
            continue
        rule_model = str(rule.get("modelId") or "")
        rule_config = rule.get("config") if isinstance(rule.get("config"), dict) else {}
        if rule_model == model_id:
            rule_config.pop("enabled", None)
            rule_config["enabled"] = True
            rule["config"] = rule_config
            seen = True
        else:
            rule_config["enabled"] = False
            rule["config"] = rule_config
    if not seen:
        provider_model_rules.append({
            "modelId": model_id,
            "config": {"enabled": True},
            "providerId": provider_id,
        })
    return data


class ZcodeAdapter(CliAgentAdapter):
    """Drive the ZCode CLI core headlessly with provider/model pinning."""

    def __init__(self, agent: AgentRecord, config: dict[str, Any] | None = None):
        super().__init__(agent, config)
        self.zcode_exe, self.zcode_cjs = resolve_zcode_install(self.config)

    # -- model pinning -----------------------------------------------------
    def _model_target(self, request: ExecutionRequest) -> tuple[str, str] | None:
        return self._model_target_for(request.model)

    def _model_target_for(self, model: str) -> tuple[str, str] | None:
        provider_config_path = resolve_provider_config_path(self.config)
        if not provider_config_path:
            return None
        try:
            source = json.loads(Path(provider_config_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(source, dict):
            return None
        return resolve_model_target(model, source)

    def _pinned_env(self, request: ExecutionRequest, log_dir: Path) -> dict[str, str]:
        return self._pinned_env_for_model(request.model, log_dir)

    def _pinned_env_for_model(self, model: str, log_dir: Path) -> dict[str, str]:
        env: dict[str, str] = {}
        target = self._model_target_for(model)
        if target is None:
            if model:
                # An explicit model that cannot be resolved must fail loudly
                # instead of silently running on providerOrder[0].
                raise ValueError(
                    f"模型 {model} 无法在 ZCode provider 配置中解析"
                    f"（{resolve_provider_config_path(self.config) or '未找到 provider_config.json'}）")
            return env
        provider_id, model_id = target
        provider_config_path = resolve_provider_config_path(self.config)
        try:
            source = json.loads(Path(provider_config_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"读取 ZCode provider 配置失败: {exc}") from exc
        pinned = build_pinned_provider_config(source, provider_id, model_id)
        pinned_path = log_dir / "zcode-provider-config.pinned.json"
        atomic_write_text(pinned_path, json.dumps(pinned, ensure_ascii=False, indent=1))
        builtin = resolve_builtin_provider_config(self.config)
        if not builtin:
            raise ValueError("未找到 ZCode builtin provider 配置（zcode-builtin.json）")
        env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"] = str(pinned_path)
        env["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] = builtin
        env["AGENTMESH_ZCODE_PINNED_PROVIDER"] = provider_id
        env["AGENTMESH_ZCODE_PINNED_MODEL"] = model_id
        return env

    # -- command -----------------------------------------------------------
    def _base_command(self, request: ExecutionRequest, instruction: str, *,
                      resume_session_id: str = "") -> list[str]:
        if not self.zcode_exe or not self.zcode_cjs:
            raise ValueError("未找到 ZCode 安装（ZCode.exe / resources\\glm\\zcode.cjs）")
        args: list[str] = [self.zcode_exe, self.zcode_cjs]
        permission_mode = str(self.config.get("permission_mode") or "yolo")
        if permission_mode:
            args += ["--mode", permission_mode]
        if resume_session_id:
            args += ["--resume", resume_session_id]
        args += ["--cwd", request.workdir, "-p", instruction, "--json"]
        extra = [str(a) for a in (self.config.get("extra_args") or [])]
        args += extra
        return args

    @staticmethod
    def _instruction(request: ExecutionRequest) -> str:
        rel_prompt = request.prompt_path
        try:
            rel_prompt = os.path.relpath(request.prompt_path, request.workdir).replace("\\", "/")
        except ValueError:
            pass
        rel_result = request.result_path
        try:
            rel_result = os.path.relpath(request.result_path, request.workdir).replace("\\", "/")
        except ValueError:
            pass
        return (
            "You are executing one task for AgentMesh OS. "
            f"Read the task package at {rel_prompt} and do exactly what it asks. "
            f"When finished, write the required JSON result to {rel_result}. "
            "Do not modify files outside the declared scope. Do not write the result anywhere else."
        )

    def _repair_instruction(self, request: ExecutionRequest) -> str:
        rel_result = request.result_path
        try:
            rel_result = os.path.relpath(request.result_path, request.workdir).replace("\\", "/")
        except ValueError:
            pass
        return (
            "Your previous run did not produce a valid JSON object at "
            f"{rel_result}. Read {request.prompt_path} again and write the required JSON "
            "result now. Keep all work already done; do not start over and do not modify "
            "files outside the declared scope."
        )

    def _base_env(self, request: ExecutionRequest) -> dict[str, str]:
        env = {
            "ELECTRON_RUN_AS_NODE": "1",
            # A headless task must not inherit an interactive Node environment.
            "NODE_OPTIONS": "",
            "AGENTLOOP_WORKDIR": request.workdir,
            "AGENTLOOP_TASK_DIR": request.task_dir,
            "AGENTLOOP_PROMPT_FILE": request.prompt_path,
            "AGENTLOOP_RESULT_FILE": request.result_path,
            "AGENTLOOP_TASK_ID": request.task.task_id,
            "AGENTLOOP_PROJECT_ROOT": request.project_root,
            "AGENTLOOP_MODEL": request.model,
        }
        retry_max = self.config.get("model_retry_max_retries", 1)
        env["ZCODE_MODEL_RETRY_MAX_RETRIES"] = str(retry_max)
        env.update(request.env)
        return env

    # -- output parsing ----------------------------------------------------
    @staticmethod
    def parse_headless_json(stdout: str, stderr: str = "") -> dict[str, Any] | None:
        """Parse the single JSON object printed by ``zcode -p --json``."""
        candidates = [stdout.strip()]
        candidates.extend(line.strip() for line in stdout.splitlines())
        if stderr.strip() and not candidates[0]:
            candidates.append(stderr.strip())
        for candidate in candidates:
            if not candidate.startswith("{"):
                continue
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
        return None

    @staticmethod
    def extract_provider_error(stderr: str) -> str:
        """Surface provider business errors (subscription/auth) verbatim."""
        for line in (stderr or "").splitlines():
            line = line.strip()
            if line.startswith("ProviderBusinessError:") or line.startswith("Error:"):
                return line[:800]
        return ""

    def _readback(self, session_id: str, workdir: str) -> dict[str, Any]:
        """Read-only verification against the ZCode session database."""
        info: dict[str, Any] = {"method": "cli", "db": ""}
        if not session_id:
            info["verify"] = {"ok": False, "reason": "no_session_id"}
            return info
        db_path = Path.home().joinpath(*DB_RELATIVE_PATH)
        info["db"] = str(db_path)
        if not db_path.exists():
            info["verify"] = {"ok": False, "reason": "db_missing"}
            return info
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
            con.row_factory = sqlite3.Row
            try:
                row = con.execute(
                    "SELECT directory, time_created FROM session WHERE id = ?", (session_id,)
                ).fetchone()
                usage = con.execute(
                    "SELECT provider_id, model_id, status, error_message FROM model_usage"
                    " WHERE session_id = ? ORDER BY started_at DESC LIMIT 1", (session_id,),
                ).fetchone()
            finally:
                con.close()
        except sqlite3.Error as exc:
            info["verify"] = {"ok": False, "reason": f"db_read_failed: {exc}"}
            return info
        session_ok = bool(row) and (not workdir or
                                    os.path.normcase(str(row["directory"] or "")) == os.path.normcase(workdir))
        info["session"] = {"directory": str(row["directory"]) if row else "",
                           "confirmed": bool(row)}
        if usage:
            info["model_request"] = {
                "provider_id": str(usage["provider_id"] or ""),
                "model_id": str(usage["model_id"] or ""),
                "status": str(usage["status"] or ""),
            }
            completed = str(usage["status"] or "") == "completed"
        else:
            info["model_request"] = {}
            completed = False
        info["verify"] = {"ok": bool(session_ok and completed),
                          "session_confirmed": session_ok,
                          "model_completed": completed}
        return info

    # -- execution ---------------------------------------------------------
    def _run_once(self, request: ExecutionRequest, instruction: str, env: dict[str, str],
                  timeout: float, *, resume_session_id: str = "",
                  log_name: str = "zcode") -> tuple[Any, dict[str, Any] | None]:
        proc = run_process(
            self._base_command(request, instruction, resume_session_id=resume_session_id),
            cwd=request.workdir, timeout=timeout, env=env,
        )
        log_dir = ensure_dir(Path(request.task_dir) / "logs")
        atomic_write_text(log_dir / f"{log_name}.stdout.log", proc.stdout)
        atomic_write_text(log_dir / f"{log_name}.stderr.log", proc.stderr)
        payload = self.parse_headless_json(proc.stdout, proc.stderr)
        return proc, payload

    @staticmethod
    def _usage_from_payload(payload: dict[str, Any] | None) -> ExecutionUsage:
        usage = ExecutionUsage()
        raw = (payload or {}).get("usage")
        if isinstance(raw, dict):
            usage.input_tokens = int(raw.get("inputTokens") or 0)
            usage.output_tokens = int(raw.get("outputTokens") or 0)
            usage.cached_tokens = int(raw.get("cacheReadTokens") or 0)
            usage.reasoning_tokens = int(raw.get("reasoningTokens") or 0)
            usage.total_tokens = int(raw.get("totalTokens") or 0)
            usage.tool_calls = int(raw.get("toolCalls") or raw.get("toolCallCount") or 0)
        usage.normalized()
        return usage

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        if not self.zcode_exe or not self.zcode_cjs or not Path(self.zcode_cjs).exists():
            return ExecutionOutcome(status="failed", error="ZCode 未安装或缺少 CLI 核心（resources\\glm\\zcode.cjs）")
        prompt_text = read_text(request.prompt_path, default="")
        instruction = self._instruction(request)
        timeout = float(self.config.get("timeout_seconds") or request.timeout_seconds)
        env = self._base_env(request)
        log_dir = ensure_dir(Path(request.task_dir) / "logs")
        try:
            env.update(self._pinned_env(request, log_dir))
        except ValueError as exc:
            return ExecutionOutcome(status="failed", error=str(exc), error_code="MODEL_UNAVAILABLE")

        resume_entry = request.resume_session if isinstance(request.resume_session, dict) else {}
        resume_session_id = ""
        if resume_entry.get("resume") and str(resume_entry.get("session_id") or "").startswith("sess_"):
            resume_session_id = str(resume_entry["session_id"])

        proc, payload = self._run_once(request, instruction, env, timeout,
                                       resume_session_id=resume_session_id)
        usage = self._usage_from_payload(payload)
        session_id = str((payload or {}).get("sessionId") or "")
        provider_error = self.extract_provider_error(proc.stderr)
        result_payload = _load_json_file(request.result_path)

        def _outcome(status: str, error: str, error_code: str = "") -> ExecutionOutcome:
            readback = self._readback(session_id, request.workdir)
            return ExecutionOutcome(
                status=status, text=str((payload or {}).get("response") or "")[-20000:],
                usage=usage, result_payload=result_payload, result_path=request.result_path,
                logs=((proc.stdout + "\n" + proc.stderr)[-30000:]),
                error=error, error_code=error_code,
                duration_ms=proc.duration_ms, pid=proc.pid, session_id=session_id,
                extras={"interface": readback},
            )

        if proc.timed_out:
            return _outcome("timeout", provider_error or "ZCode 执行超时", "EXEC_TIMEOUT")
        if proc.exit_code != 0:
            return _outcome("failed",
                            provider_error or f"ZCode 退出码 {proc.exit_code}: {(proc.stderr or proc.stdout).strip()[:400]}",
                            "PROVIDER_BUSINESS_ERROR" if provider_error else "CLI_EXIT_NONZERO")
        if not payload or not session_id:
            return _outcome("failed", "ZCode 未返回可解析的 JSON 结果", "CLI_OUTPUT_UNPARSABLE")
        if provider_error:
            return _outcome("failed", provider_error, "PROVIDER_BUSINESS_ERROR")

        if result_payload is None and int(self.config.get("repair_attempts", 1)) > 0:
            repair_proc, repair_payload = self._run_once(
                request, self._repair_instruction(request), env, min(timeout, 600.0),
                resume_session_id=session_id or resume_session_id, log_name="zcode.repair")
            repair_usage = self._usage_from_payload(repair_payload)
            usage.input_tokens += repair_usage.input_tokens
            usage.output_tokens += repair_usage.output_tokens
            usage.cached_tokens += repair_usage.cached_tokens
            usage.reasoning_tokens += repair_usage.reasoning_tokens
            usage.total_tokens += repair_usage.total_tokens
            usage.tool_calls += repair_usage.tool_calls
            usage.normalized()
            if str((repair_payload or {}).get("sessionId") or ""):
                session_id = str(repair_payload["sessionId"])
            result_payload = _load_json_file(request.result_path)

        readback = self._readback(session_id, request.workdir)
        return ExecutionOutcome(
            status="completed", text=str((payload or {}).get("response") or "")[-20000:],
            usage=usage, tool_calls=[], result_payload=result_payload,
            result_path=request.result_path,
            logs=((proc.stdout + "\n" + proc.stderr)[-30000:]),
            duration_ms=proc.duration_ms, pid=proc.pid, session_id=session_id,
            extras={"interface": readback, "result_written": result_payload is not None},
        )

    def probe_model(self, model: str, *, timeout: float = 90.0) -> dict[str, Any]:
        """Prove a pinned model answers with a minimal headless run.

        Used by catalog sync to detect expired subscriptions before the
        planner binds the model (2026-11-07 defect #3).  The probe runs in a
        throwaway directory with a one-line prompt and zero model retries; it
        never touches the user's provider config or project files.
        """
        import tempfile

        from ..control.model_health import classify_model_error
        if not self.zcode_exe or not self.zcode_cjs or not Path(self.zcode_cjs).exists():
            return {"ok": False, "status": "unknown", "reason": "not_installed"}
        workdir = tempfile.mkdtemp(prefix="agentmesh-zcode-probe-")
        log_dir = ensure_dir(Path(workdir) / "logs")
        try:
            pin = self._pinned_env_for_model(model, log_dir)
        except ValueError as exc:
            return {"ok": False, "status": "unknown", "reason": str(exc)[:300]}
        env = {"ELECTRON_RUN_AS_NODE": "1", "NODE_OPTIONS": "",
               "ZCODE_MODEL_RETRY_MAX_RETRIES": "0", **pin}
        args = [self.zcode_exe, self.zcode_cjs, "--mode",
                str(self.config.get("permission_mode") or "yolo"),
                "--cwd", workdir, "-p",
                "Health probe: reply with exactly OK. Do not use any tool.",
                "--json"]
        try:
            proc = run_process(args, cwd=workdir, timeout=float(timeout), env=env)
        except Exception as exc:
            return {"ok": False, "status": "unknown", "reason": str(exc)[:300]}
        atomic_write_text(log_dir / "probe.stdout.log", proc.stdout)
        atomic_write_text(log_dir / "probe.stderr.log", proc.stderr)
        provider_error = self.extract_provider_error(proc.stderr)
        payload = self.parse_headless_json(proc.stdout, proc.stderr)
        if proc.exit_code == 0 and not proc.timed_out and payload \
                and payload.get("sessionId") and not provider_error:
            return {"ok": True, "status": "ok",
                    "session_id": str(payload.get("sessionId") or ""),
                    "pinned_provider": pin.get("AGENTMESH_ZCODE_PINNED_PROVIDER", ""),
                    "pinned_model": pin.get("AGENTMESH_ZCODE_PINNED_MODEL", "")}
        error = provider_error or (proc.stderr or proc.stdout).strip()[-400:]
        kind = classify_model_error(error)
        return {"ok": False, "status": "degraded" if kind else "unknown",
                "reason": kind or ("timeout" if proc.timed_out else "probe_failed"),
                "error": error[:300],
                "pinned_provider": pin.get("AGENTMESH_ZCODE_PINNED_PROVIDER", ""),
                "pinned_model": pin.get("AGENTMESH_ZCODE_PINNED_MODEL", "")}

    def health(self) -> dict[str, Any]:
        available = bool(self.zcode_exe) and bool(self.zcode_cjs) and Path(self.zcode_cjs).exists()
        return {
            "agent_id": self.agent.agent_id, "adapter_type": "zcode",
            "executable": self.zcode_exe, "cli_core": self.zcode_cjs,
            "available": available,
            "status": "online" if available else "offline",
        }
