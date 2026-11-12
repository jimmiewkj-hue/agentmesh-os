"""DeepSeek Harness (dsh) headless CLI adapter.

Every mechanism below was verified against a real run on this machine
(DSH 0.2.0-rc.2), not assumed:

  * Trigger: the desktop install ships a headless profile booted through the
    Electron binary in Node mode, zero GUI / zero OS input::

      ELECTRON_RUN_AS_NODE=1 "DeepSeek Harness.exe" --expose-internals
        resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js
        --profile headless [--patch <model.yml>] --json [--session-id <id>] "<task>"

  * Credentials: the headless profile does not materialize the credentials
    service refs on its own; a missing key fails with MISSING_CREDENTIAL. The
    adapter therefore loads ``~/.dsh/.credentials.yaml`` ``refs`` (the user's
    own provider keys, e.g. DEEPSEEK_API_KEY) into the subprocess environment,
    exactly as the error message instructs ("export DEEPSEEK_API_KEY in the
    launching environment"). Keys are never logged.
  * Model selection: ``--model`` is NOT a valid option. The model is the
    ``agent-default-model`` plugin config; a launcher ``--patch`` overlay
    (placed *before* the app flag ``--json``) pins ``provider``/``model``.
    Verified: a bogus model yields INVALID_REQUEST listing the real supported
    names (deepseek-official: deepseek-flash, deepseek-v4-pro).
  * Events (``--json``): ``session`` (sessionId/cwd), ``status`` turn/step
    phases — ``step_end.usage`` carries camelCase token counts
    (inputTokens/outputTokens/cacheReadTokens/cacheWriteTokens/totalTokens) —
    ``text``/``thinking`` deltas, ``final`` (text), and ``error`` (message) for
    CLI argument errors. ``turn_end.reason.kind == "error"`` carries provider
    failures. ``--session-id`` resumes a persisted session (verified to recall
    prior context); an unknown id is an error.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from ..models import AgentRecord
from ..util import atomic_write_text, ensure_dir
from .base import ExecutionOutcome, ExecutionRequest, ExecutionUsage
from .cli import CliAgentAdapter, _load_json_file
from .process import run_process

CLI_RELATIVE_PATH = ("resources", "app.asar", "dsh", "node_modules",
                     "@deepseek-ai", "dsh-desktop-host", "lib", "cli.js")
CREDENTIALS_RELATIVE_PATH = (".dsh", ".credentials.yaml")


def resolve_dsh_install(config: dict[str, Any] | None = None) -> tuple[str, str]:
    """Return ``(dsh_exe, cli_js)``; either may be empty when missing.

    ``cli.js`` lives inside ``app.asar`` and cannot be stat'd from the filesystem;
    the Electron binary reads it directly, so only the exe and the asar archive
    are checked for existence.
    """
    cfg = dict(config or {})
    exe = str(cfg.get("dsh_exe") or cfg.get("executable") or "")
    if exe and not Path(exe).exists():
        exe = ""
    if not exe:
        local = Path(os.environ.get("LOCALAPPDATA") or "") / "Programs" / "DeepSeek Harness" / "DeepSeek Harness.exe"
        pf = Path(os.environ.get("PROGRAMFILES") or "") / "DeepSeek Harness" / "DeepSeek Harness.exe"
        for candidate in (local, pf):
            if candidate.exists():
                exe = str(candidate)
                break
    cli = str(cfg.get("dsh_cli") or "")
    if not cli and exe:
        asar = Path(exe).parent / "resources" / "app.asar"
        if asar.exists():
            cli = str(Path(exe).parent.joinpath(*CLI_RELATIVE_PATH))
    return exe, cli


def load_dsh_credentials(path: Path | None = None) -> dict[str, str]:
    """Read the user's dsh credentials-service refs (provider API keys).

    These are the keys the dsh web app stores; the headless profile needs them
    exported into the environment. Values are returned for subprocess env only
    and must never be logged or written to disk by the adapter.
    """
    path = path or Path.home().joinpath(*CREDENTIALS_RELATIVE_PATH)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return {}
    refs = data.get("refs") if isinstance(data, dict) else None
    if not isinstance(refs, dict):
        return {}
    return {str(k): str(v) for k, v in refs.items() if v}


class DshAdapter(CliAgentAdapter):
    def __init__(self, agent: AgentRecord, config: dict[str, Any] | None = None):
        super().__init__(agent, config)
        self.dsh_exe, self.dsh_cli = resolve_dsh_install(self.config)
        self.profile = str(self.config.get("profile") or "headless")
        self.provider = str(self.config.get("provider") or "deepseek-official")

    def _model_patch(self, request: ExecutionRequest, log_dir: Path) -> str:
        """Write a launcher --patch overlay pinning agent-default-model.

        Returns the patch path, or "" when no model is requested (profile
        default applies). The patch is kept in the log dir as execution evidence.
        """
        model = str(request.model or "").strip()
        if not model:
            return ""
        # A provider-prefixed id (provider/model) overrides the configured provider.
        provider = self.provider
        if "/" in model:
            provider, model = model.split("/", 1)
        patch = [{"id": "agent-default-model",
                  "config": {"provider": provider, "model": model}}]
        patch_path = log_dir / "dsh-model-patch.yml"
        atomic_write_text(patch_path, yaml.safe_dump(patch, sort_keys=False))
        return str(patch_path)

    def _base_command(self, request: ExecutionRequest, instruction: str, *,
                      resume_session_id: str = "", patch_path: str = "") -> list[str]:
        if not self.dsh_exe or not self.dsh_cli:
            raise ValueError("未找到 DeepSeek Harness 安装（DeepSeek Harness.exe / resources/app.asar）")
        args: list[str] = [self.dsh_exe, "--expose-internals", self.dsh_cli,
                           "--profile", self.profile]
        # --patch is a launcher flag and must precede the app flag --json.
        if patch_path:
            args += ["--patch", patch_path]
        args.append("--json")
        if resume_session_id:
            args += ["--session-id", resume_session_id]
        args.append(instruction)
        args += [str(a) for a in (self.config.get("extra_args") or [])]
        return args

    @staticmethod
    def _instruction(request: ExecutionRequest) -> str:
        rel_prompt = request.prompt_path
        rel_result = request.result_path
        try:
            rel_prompt = os.path.relpath(request.prompt_path, request.workdir).replace("\\", "/")
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
            "NODE_OPTIONS": "",
            "AGENTLOOP_WORKDIR": request.workdir,
            "AGENTLOOP_TASK_DIR": request.task_dir,
            "AGENTLOOP_PROMPT_FILE": request.prompt_path,
            "AGENTLOOP_RESULT_FILE": request.result_path,
            "AGENTLOOP_TASK_ID": request.task.task_id,
            "AGENTLOOP_PROJECT_ROOT": request.project_root,
            "AGENTLOOP_MODEL": request.model,
        }
        # Materialize the user's dsh provider keys (never logged). Existing
        # process env and explicit request.env take precedence over the refs.
        for key, value in load_dsh_credentials().items():
            env.setdefault(key, value)
        env.update(request.env)
        return env

    @staticmethod
    def parse_events(stdout: str) -> dict[str, Any]:
        """Parse the real dsh ``--json`` newline-delimited run events."""
        session_id = ""
        final_text = ""
        text_parts: list[str] = []
        error = ""
        error_code = ""
        usage = ExecutionUsage(currency="", cost_known=False, estimated=True)
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "session":
                session_id = str(event.get("sessionId") or session_id)
            elif kind == "text":
                text_parts.append(str(event.get("text") or ""))
            elif kind == "final":
                final_text = str(event.get("text") or final_text)
            elif kind == "error":
                error = str(event.get("message") or error)
                error_code = error_code or "CLI_ERROR"
            elif kind == "status":
                tokens = event.get("usage") or {}
                if isinstance(tokens, dict) and tokens:
                    usage.input_tokens += int(tokens.get("inputTokens", 0) or 0)
                    usage.output_tokens += int(tokens.get("outputTokens", 0) or 0)
                    usage.cached_tokens += int(tokens.get("cacheReadTokens", 0) or 0)
                    usage.total_tokens += int(tokens.get("totalTokens", 0) or 0)
                reason = event.get("reason") or {}
                if isinstance(reason, dict) and reason.get("kind") == "error":
                    err = reason.get("error") or {}
                    if isinstance(err, dict):
                        error = str(err.get("message") or error)
                        error_code = str(err.get("code") or error_code)
        usage.normalized()
        return {"session_id": session_id, "text": final_text or "".join(text_parts),
                "error": error, "error_code": error_code, "usage": usage}

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        if not self.dsh_exe or not self.dsh_cli:
            return ExecutionOutcome(status="failed", error="DeepSeek Harness 未安装或不可执行")
        instruction = self._instruction(request)
        timeout = float(self.config.get("timeout_seconds") or request.timeout_seconds)
        resume_entry = request.resume_session if isinstance(request.resume_session, dict) else {}
        resume_session_id = ""
        if (resume_entry.get("resume")
                and str(resume_entry.get("session_id") or "").startswith("session-")
                and bool(self.config.get("chain_reuse_agent_sessions", True))):
            resume_session_id = str(resume_entry["session_id"])
        env = self._base_env(request)
        log_dir = ensure_dir(Path(request.task_dir) / "logs")
        patch_path = self._model_patch(request, log_dir)

        proc = run_process(
            self._base_command(request, instruction, resume_session_id=resume_session_id,
                               patch_path=patch_path),
            cwd=request.workdir, timeout=timeout, env=env,
        )
        atomic_write_text(log_dir / "dsh.stdout.log", proc.stdout)
        atomic_write_text(log_dir / "dsh.stderr.log", proc.stderr)
        parsed = self.parse_events(proc.stdout)
        session_id = parsed["session_id"]
        usage = parsed["usage"]
        payload = _load_json_file(request.result_path)
        stderr = (proc.stderr or "").strip()
        error = parsed["error"]
        error_code = parsed["error_code"]
        # Provider business errors arrive on stderr (e.g. INVALID_REQUEST for an
        # unsupported model, MISSING_CREDENTIAL for a missing key).
        if not error and stderr:
            error = stderr[:800]
        if "MISSING_CREDENTIAL" in stderr or error_code == "MISSING_CREDENTIAL":
            error_code = "MISSING_CREDENTIAL"
        elif "INVALID_REQUEST" in stderr and "model" in stderr.lower():
            error_code = "MODEL_UNAVAILABLE"

        def outcome(status: str, err: str, code: str = "") -> ExecutionOutcome:
            return ExecutionOutcome(
                status=status, text=parsed["text"][-20000:], usage=usage,
                result_payload=payload, result_path=request.result_path,
                logs=((proc.stdout + "\n" + proc.stderr)[-30000:]),
                error=err, error_code=code,
                duration_ms=proc.duration_ms, pid=proc.pid, session_id=session_id,
                extras={"interface": {"resumed_session_id":
                                      resume_session_id if resume_session_id
                                      and session_id == resume_session_id else ""}},
            )

        if proc.timed_out:
            return outcome("timeout", error or "dsh 执行超时", "EXEC_TIMEOUT")
        if error_code == "MISSING_CREDENTIAL":
            return outcome("failed", error or "dsh 缺少凭据：请在 ~/.dsh/.credentials.yaml 配置 DEEPSEEK_API_KEY",
                           "MISSING_CREDENTIAL")
        if proc.exit_code != 0 or error:
            return outcome("failed", error or f"dsh 退出码 {proc.exit_code}",
                           error_code or "CLI_EXIT_NONZERO")

        repair_logs = ""
        if payload is None and int(self.config.get("repair_attempts", 1)) > 0:
            repair = run_process(
                self._base_command(request, self._repair_instruction(request),
                                   resume_session_id=session_id or resume_session_id,
                                   patch_path=patch_path),
                cwd=request.workdir, timeout=min(timeout, 600), env=env,
            )
            repair_logs = repair.stdout + "\n" + repair.stderr
            atomic_write_text(log_dir / "dsh.repair.stdout.log", repair.stdout)
            repair_parsed = self.parse_events(repair.stdout)
            usage.input_tokens += repair_parsed["usage"].input_tokens
            usage.output_tokens += repair_parsed["usage"].output_tokens
            usage.cached_tokens += repair_parsed["usage"].cached_tokens
            usage.total_tokens += repair_parsed["usage"].total_tokens
            usage.normalized()
            session_id = repair_parsed["session_id"] or session_id
            payload = _load_json_file(request.result_path)

        result = outcome("completed", "")
        result.logs = ((proc.stdout + "\n" + proc.stderr + "\n" + repair_logs)[-30000:])
        result.extras["result_written"] = payload is not None
        return result

    def health(self) -> dict[str, Any]:
        available = bool(self.dsh_exe and self.dsh_cli)
        return {
            "agent_id": self.agent.agent_id, "adapter_type": "dsh",
            "executable": self.dsh_exe, "available": available,
            "status": "online" if available else "offline",
        }