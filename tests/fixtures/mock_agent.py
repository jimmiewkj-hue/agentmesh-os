"""Deterministic mock Agent used by integration tests.

It implements the same file contract as a real CLI adapter:
  * read AGENTLOOP_PROMPT_FILE / AGENTLOOP_RESULT_FILE / AGENTLOOP_TASK_ID
  * perform the actions described in a scenario JSON
  * write one structured JSON result
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


REQUIRED = [
    "task_id", "agent_id", "status", "summary", "findings", "files_read",
    "files_changed", "commands_run", "tool_calls", "tests", "evidence", "risks",
    "unresolved_items", "artifacts", "token_usage", "cost_estimate",
    "recommended_next_action",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--prompt-file", default="")
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--task-id", default="")
    parser.add_argument("--agent-id", default="")
    args = parser.parse_args()

    task_id = args.task_id or os.environ.get("AGENTLOOP_TASK_ID", "")
    agent_id = args.agent_id or os.environ.get("MOCK_AGENT_ID", "mock")
    scenario_path = os.environ.get("MOCK_SCENARIO_FILE", "")
    scenario = {}
    if scenario_path and Path(scenario_path).exists():
        scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8"))
    action = {}
    if isinstance(scenario.get("tasks"), dict):
        action = scenario["tasks"].get(task_id, {}) or {}
    sleep = float(action.get("sleep", scenario.get("sleep", 0)) or 0)
    if sleep:
        time.sleep(sleep)
    workdir = Path(args.workdir)
    result_path = Path(args.result_file)
    result_path.parent.mkdir(parents=True, exist_ok=True)

    prompt = ""
    if args.prompt_file and Path(args.prompt_file).exists():
        prompt = Path(args.prompt_file).read_text(encoding="utf-8", errors="replace")

    basename = result_path.name.lower()
    if basename == "plan.json":
        sequence = scenario.get("plan_sequence")
        if sequence:
            marker_dir = Path(os.environ.get("AGENTLOOP_TASK_DIR", workdir))
            marker_dir.mkdir(parents=True, exist_ok=True)
            marker = marker_dir / ".plan_attempt"
            attempt = int(marker.read_text(encoding="utf-8")) if marker.exists() else 0
            marker.write_text(str(attempt + 1), encoding="utf-8")
            plan = sequence[min(attempt, len(sequence) - 1)]
        elif "多轮续跑上下文" in prompt and scenario.get("round_plan"):
            plan = scenario["round_plan"]
        else:
            plan = scenario.get("plan")
        if not plan:
            plan = {
                "plan_version": 1,
                "goal": "mock plan",
                "constraints": [],
                "tasks": [{
                    "task_id": "T001", "title": "mock", "type": "analysis",
                    "objective": "mock", "scope": [], "dependencies": [],
                }],
            }
        result_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print("mock plan written")
        return 0

    if basename == "summary.json":
        if scenario.get("summary_missing"):
            print("mock summary missing")
            return 0
        sequence = scenario.get("summary_sequence")
        if sequence:
            marker_dir = Path(os.environ.get("AGENTLOOP_TASK_DIR", workdir))
            marker_dir.mkdir(parents=True, exist_ok=True)
            marker = marker_dir / ".summary_attempt"
            attempt = int(marker.read_text(encoding="utf-8")) if marker.exists() else 0
            marker.write_text(str(attempt + 1), encoding="utf-8")
            summary = sequence[min(attempt, len(sequence) - 1)]
        else:
            summary = scenario.get("summary") or {
                "summary_version": 1, "completion": "mock completed", "changes": [],
                "verification": "passed", "risks": [], "merge_recommendation": "approve",
                "next_actions": [],
            }
        result_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print("mock summary written")
        return 0

    if basename == "verification.json":
        verification = scenario.get("verification") or {}
        verification.setdefault("task_id", task_id)
        verification.setdefault("verifier_agent", agent_id)
        verification.setdefault("verification_status", "passed")
        verification.setdefault("checks", {"result_schema": "passed", "scope_check": "passed"})
        verification.setdefault("warnings", [])
        result_path.write_text(json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8")
        print("mock verification written")
        return 0

    # Task mode
    writes = action.get("write_files") or []
    files_changed = []
    for item in writes:
        rel = str(item["path"])
        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(item.get("content", "")), encoding="utf-8")
        files_changed.append(rel)
    delete = action.get("delete_files") or []
    for rel in delete:
        target = workdir / str(rel)
        if target.exists():
            target.unlink()
            files_changed.append(str(rel))
    fail_marker = action.get("fail_once_marker")
    if fail_marker:
        # Keep the marker out of the worktree so it never becomes a code change.
        marker_dir = Path(os.environ.get("AGENTLOOP_TASK_DIR", workdir))
        marker = marker_dir / fail_marker
        marker.parent.mkdir(parents=True, exist_ok=True)
        if not marker.exists():
            marker.write_text("failed once", encoding="utf-8")
            print("mock intentional failure", file=sys.stderr)
            return 1

    payload = {key: ([] if key in {
        "findings", "files_read", "files_changed", "commands_run", "tool_calls", "tests",
        "evidence", "risks", "unresolved_items", "artifacts", "recommended_next_action",
    } else "" ) for key in REQUIRED}
    payload.update({
        "task_id": task_id,
        "agent_id": agent_id,
        "status": action.get("status", "completed"),
        "summary": action.get("summary", f"mock did {task_id}"),
        "files_changed": files_changed,
        "evidence": action.get("evidence", [{"file": files_changed[0], "lines": "1-10",
                                              "description": "mock change"} ] if files_changed else []),
        "tests": action.get("tests", []),
        "unresolved_items": action.get("unresolved_items", []),
        "risks": action.get("risks", []),
        "findings": action.get("findings", []),
        "files_read": action.get("files_read", []),
        "commands_run": action.get("commands_run", []),
        "tool_calls": action.get("tool_calls", []),
        "artifacts": action.get("artifacts", []),
        "token_usage": action.get("token_usage", {"input": 1000, "output": 200}),
        "cost_estimate": action.get("cost_estimate", {"amount": 0, "currency": "USD", "estimated": True}),
        "recommended_next_action": action.get("recommended_next_action", []),
    })
    payload.update({k: v for k, v in action.get("result", {}).items()})
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # Record the model the control plane actually dispatched with, so tests can
    # prove per-task model routing without depending on stdout parsing.
    model = os.environ.get("AGENTLOOP_MODEL", "")
    if model:
        marker_dir = Path(os.environ.get("AGENTLOOP_TASK_DIR", workdir))
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / "dispatched-model.txt").write_text(model, encoding="utf-8")
    print(f"mock completed {task_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
