"""Real-machine ZCode headless chain verification.

Runs the ZcodeAdapter against the real ZCode CLI core (ELECTRON_RUN_AS_NODE +
resources/glm/zcode.cjs) with a real provider/model and verifies:
  1. discovery upgrades zcode to L3 (adapter_type=zcode) on this machine;
  2. one real task completes: bug fixed in the repo, result.json written;
  3. readback: session row + model_usage row in ~/.zcode/cli/db/db.sqlite
     confirm session/workdir/model/provider;
  4. the user's real provider_config.json is byte-identical before/after.

Evidence: verification/zcode-chain-evidence.json (+ logs next to it).

Usage: python verification/verify_zcode_real.py [--model deepseek-v4-pro]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentloop_hub.adapters.base import ExecutionRequest  # noqa: E402
from agentloop_hub.adapters.discovery import discover_agents  # noqa: E402
from agentloop_hub.adapters.zcode import ZcodeAdapter  # noqa: E402
from agentloop_hub.models import Task, TaskSpec  # noqa: E402

CALC_BUGGY = "def add(a, b):\n    return a - b\n"
TEST_CALC = (
    "from calc import add\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n"
)
PROMPT = """# 任务 T001：修复 calc.py 的加法缺陷（AgentMesh OS 控制平面派发）

工作区（当前目录）内的 `calc.py` 中 `add(a, b)` 误写为 `return a - b`。
要求：
1. 修复 `calc.py`，使 `add(2, 3) == 5`；
2. 在当前目录运行 `python test_calc.py` 验证通过；
3. **必须**将成果 JSON 写入 `result.json`（当前目录），结构：
   {"summary": "一句话结论", "findings": [{"file": "calc.py", "lines": "1-2",
     "description": "缺陷与修复说明"}], "commands_run": ["python test_calc.py"],
   "evidence": [{"file": "calc.py", "lines": "1-2", "description": "修复后内容要点"}]}
不要修改 `result.json` 与任务包以外的任何文件。
"""


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    evidence: dict = {"generated_at": datetime.now(timezone.utc).isoformat(),
                      "model": args.model, "steps": []}

    # -- 1. real discovery --------------------------------------------------
    records = discover_agents("zcode-real-verify")
    zcode = next((r for r in records if r.agent_id == "zcode"), None)
    assert zcode is not None, "discovery did not produce a zcode record"
    evidence["steps"].append({
        "step": "discovery",
        "adapter_type": zcode.adapter_type,
        "access_level": zcode.access_level,
        "zcode_exe": zcode.adapter_config.get("zcode_exe"),
        "zcode_cjs": zcode.adapter_config.get("zcode_cjs"),
        "models": zcode.models,
    })
    assert zcode.adapter_type == "zcode", \
        f"expected zcode L3 adapter, got {zcode.adapter_type}"
    assert zcode.adapter_config.get("zcode_cjs"), "CLI core not found"

    # -- 2. real task workspace ---------------------------------------------
    base = ROOT / "verification" / "zcode-chain-real"
    if base.exists():
        import shutil
        shutil.rmtree(base)
    repo = base / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    (repo / "calc.py").write_text(CALC_BUGGY, encoding="utf-8")
    (repo / "test_calc.py").write_text(TEST_CALC, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=agentmesh@local", "-c", "user.name=agentmesh",
        "commit", "-qm", "seed: buggy calc")
    task_dir = repo / ".agentloop-task"
    task_dir.mkdir()
    prompt_path = task_dir / "prompt.md"
    prompt_path.write_text(PROMPT, encoding="utf-8")
    result_path = task_dir / "result.json"

    provider_config = Path(zcode.adapter_config["provider_config"])
    before = provider_config.read_bytes() if provider_config.exists() else b""

    task = Task(uid="zcodeverify:T001", project_id="zcode-real-verify", plan_id="",
                run_id="zcodeverify",
                spec=TaskSpec(task_id="T001", title="修复 calc 加法缺陷",
                              objective="fix add()", type="implementation",
                              scope=["calc.py"], token_budget=100000,
                              time_limit_minutes=15, retry_limit=0),
                status="running", assigned_agent="zcode", base_commit="HEAD")
    request = ExecutionRequest(
        project_id="zcode-real-verify", project_root=str(repo), task=task,
        workdir=str(repo), task_dir=str(task_dir), prompt_path=str(prompt_path),
        result_path=str(result_path), model=args.model,
        timeout_seconds=args.timeout, purpose="task",
    )

    adapter = ZcodeAdapter(zcode)
    health = adapter.health()
    evidence["steps"].append({"step": "health", **health})
    assert health["available"], f"zcode adapter not available: {health}"

    started = time.monotonic()
    outcome = adapter.execute(request)
    duration_ms = int((time.monotonic() - started) * 1000)

    # -- 3. assertions -------------------------------------------------------
    fixed = "return a + b" in (repo / "calc.py").read_text(encoding="utf-8")
    test_ok = subprocess.run([sys.executable, "test_calc.py"], cwd=repo,
                             capture_output=True, text=True).returncode == 0
    after = provider_config.read_bytes() if provider_config.exists() else b""

    evidence["steps"].append({
        "step": "execute",
        "status": outcome.status,
        "session_id": outcome.session_id,
        "duration_ms": outcome.duration_ms,
        "wall_ms": duration_ms,
        "usage": outcome.usage.to_dict(),
        "result_payload": outcome.result_payload,
        "response_text": (outcome.text or "")[:800],
        "error": outcome.error,
        "error_code": outcome.error_code,
        "interface": outcome.extras.get("interface"),
    })
    evidence["steps"].append({
        "step": "workspace",
        "calc_fixed": fixed,
        "test_passed": test_ok,
        "calc_content": (repo / "calc.py").read_text(encoding="utf-8"),
    })
    evidence["steps"].append({
        "step": "provider_config_untouched",
        "ok": before == after,
    })

    problems: list[str] = []
    if outcome.status != "completed":
        problems.append(f"status={outcome.status} error={outcome.error}")
    if not outcome.result_payload:
        problems.append("result.json missing")
    if not fixed:
        problems.append("calc.py not fixed")
    if not test_ok:
        problems.append("test_calc.py failed")
    if before != after:
        problems.append("real provider_config.json was modified")
    interface = (outcome.extras or {}).get("interface") or {}
    verify = interface.get("verify") or {}
    if not verify.get("ok"):
        problems.append(f"readback verify failed: {json.dumps(verify, ensure_ascii=False)}")
    else:
        model_request = interface.get("model_request") or {}
        evidence["steps"].append({"step": "readback", "model_request": model_request,
                                  "session": interface.get("session")})
        if model_request.get("status") != "completed":
            problems.append("model_usage status != completed")
    if problems:
        evidence["problems"] = problems
        out = ROOT / "verification" / "zcode-chain-evidence.json"
        out.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"FAILED: {problems}")
        print(f"evidence: {out}")
        return 1

    evidence["conclusion"] = "zcode headless chain verified end-to-end"
    out = ROOT / "verification" / "zcode-chain-evidence.json"
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"OK  status={outcome.status} session={outcome.session_id} "
          f"duration={outcome.duration_ms}ms model={args.model}")
    print(f"evidence: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
