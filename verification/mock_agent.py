"""Independent mock Agent for requirement verification.

Implements only the public file contract documented in the project README:

    AGENTLOOP_WORKDIR / AGENTLOOP_PROMPT_FILE / AGENTLOOP_RESULT_FILE /
    AGENTLOOP_TASK_ID  ->  the Agent writes one result JSON object.

Behaviour is driven by environment variables so no project test helper is used:

    MOCK_AGENT_ID     Agent identity echoed into result.json
    MOCK_PLAN_FILE    when result path basename is plan.json, copy this file
    MOCK_SUMMARY_FILE when result path basename is summary.json, copy this file
    MOCK_SCENARIO     JSON dict: task_id -> action
    MOCK_FAIL_TIMES   fail this many attempts before succeeding
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

LIST_FIELDS = (
    "findings", "files_read", "files_changed", "commands_run", "tool_calls",
    "tests", "evidence", "risks", "unresolved_items", "artifacts",
    "recommended_next_action",
)


def _result(payload: dict) -> dict:
    out = {
        "task_id": payload.get("task_id", ""),
        "agent_id": payload.get("agent_id", ""),
        "status": payload.get("status", "completed"),
        "summary": payload.get("summary", "done"),
        "token_usage": payload.get("token_usage", {"input": 100, "output": 50, "total": 150}),
        "cost_estimate": payload.get("cost_estimate", {"amount": 0.01, "currency": "USD",
                                                       "estimated": True}),
    }
    for key in LIST_FIELDS:
        out[key] = payload.get(key, [])
    return out


def main() -> int:
    result_path = Path(os.environ["AGENTLOOP_RESULT_FILE"])
    task_id = os.environ.get("AGENTLOOP_TASK_ID", "")
    agent_id = os.environ.get("MOCK_AGENT_ID", "mock")
    workdir = Path(os.environ.get("AGENTLOOP_WORKDIR", "."))
    result_path.parent.mkdir(parents=True, exist_ok=True)

    basename = result_path.name.lower()
    trace = os.environ.get("MOCK_TRACE", "")
    if trace:
        with open(trace, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "task_id": task_id, "agent": agent_id, "basename": basename,
                "cwd": os.getcwd(), "workdir": str(workdir),
            }, ensure_ascii=False) + "\n")
    scenario = {}
    scenario_file = os.environ.get("MOCK_SCENARIO_FILE", "")
    if scenario_file and Path(scenario_file).exists():
        scenario = json.loads(Path(scenario_file).read_text(encoding="utf-8") or "{}")
    elif os.environ.get("MOCK_SCENARIO"):
        scenario = json.loads(os.environ["MOCK_SCENARIO"])

    if basename == "plan.json":
        plan_file = os.environ.get("MOCK_PLAN_FILE", "")
        if not plan_file or not Path(plan_file).exists():
            print("no plan file", file=sys.stderr)
            return 3
        result_path.write_text(Path(plan_file).read_text(encoding="utf-8"), encoding="utf-8")
        return 0
    if basename == "verification.json":
        verify_action = scenario.get("__verify__", {}) if isinstance(scenario, dict) else {}
        for item in verify_action.get("write_files", []) or []:
            target = workdir / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item.get("content", ""), encoding="utf-8")
        report = {
            "task_id": task_id,
            "verifier_agent": agent_id,
            "verification_status": verify_action.get("verification_status", "passed"),
            "checks": verify_action.get("checks", {"independent_review": "passed"}),
            "warnings": verify_action.get("warnings", []),
            "summary": verify_action.get("summary", "independent verification passed"),
            "evidence": verify_action.get("evidence", []),
        }
        result_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return 0
    if basename == "summary.json":
        summary_file = os.environ.get("MOCK_SUMMARY_FILE", "")
        if not summary_file or not Path(summary_file).exists():
            print("no summary file", file=sys.stderr)
            return 3
        result_path.write_text(Path(summary_file).read_text(encoding="utf-8"), encoding="utf-8")
        return 0

    action = scenario.get(task_id, {}) if isinstance(scenario, dict) else {}
    time.sleep(float(action.get("sleep", 0) or 0))

    # Attempt counter lives in the task dir so retries can be observed.
    task_dir = Path(os.environ.get("AGENTLOOP_TASK_DIR", str(result_path.parent)))
    counter = task_dir / f"attempts-{task_id}-{agent_id}.txt"
    attempts = 0
    if counter.exists():
        try:
            attempts = int(counter.read_text(encoding="utf-8").strip() or 0)
        except ValueError:
            attempts = 0
    attempts += 1
    task_dir.mkdir(parents=True, exist_ok=True)
    counter.write_text(str(attempts), encoding="utf-8")

    fail_times = int(os.environ.get("MOCK_FAIL_TIMES", "0") or 0)
    if attempts <= fail_times:
        print(f"intentional failure attempt {attempts}", file=sys.stderr)
        return 1

    for item in action.get("write_files", []) or []:
        target = workdir / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item.get("content", ""), encoding="utf-8")

    if action.get("exit_code"):
        return int(action["exit_code"])

    payload = {
        "task_id": task_id,
        "agent_id": agent_id,
        "status": action.get("status", "completed"),
        "summary": action.get("summary", f"{task_id} done by {agent_id}"),
        "findings": action.get("findings", []),
        "files_read": action.get("files_read", []),
        "files_changed": action.get("files_changed", [f["path"] for f in action.get("write_files", [])]),
        "commands_run": action.get("commands_run", []),
        "tool_calls": action.get("tool_calls", []),
        "tests": action.get("tests", []),
        "evidence": action.get("evidence", []),
        "risks": action.get("risks", []),
        "unresolved_items": action.get("unresolved_items", []),
        "artifacts": action.get("artifacts", []),
        "token_usage": action.get("token_usage", {"input": 120, "output": 60, "total": 180}),
        "cost_estimate": action.get("cost_estimate",
                                    {"amount": 0.02, "currency": "USD", "estimated": True}),
        "recommended_next_action": action.get("recommended_next_action", []),
    }
    result_path.write_text(json.dumps(_result(payload), ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
