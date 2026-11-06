"""Read-only nodes that produce files must hand them to downstream nodes."""

from __future__ import annotations

from pathlib import Path

from hub_test_support import make_project


def _docs_plan() -> dict:
    return {
        "plan_version": 1,
        "goal": "research first, then summarize",
        "constraints": [],
        "tasks": [
            {
                "task_id": "R001", "title": "research", "type": "analysis",
                "objective": "write the reference document", "suggested_agent": "mock_worker",
                "dependencies": [], "scope": ["docs/spec.md"],
                "out_of_scope": [], "expected_outputs": ["docs/spec.md"],
                "acceptance_criteria": ["document exists"], "verification_commands": [],
                "token_budget": 5000, "time_limit_minutes": 5, "retry_limit": 1,
                "risk_level": "low",
            },
            {
                "task_id": "R002", "title": "summary", "type": "analysis",
                "objective": "read the document", "suggested_agent": "mock_verifier",
                "dependencies": [], "scope": [], "out_of_scope": [],
                "expected_outputs": ["summary"], "acceptance_criteria": ["report"],
                "verification_commands": [], "token_budget": 5000,
                "time_limit_minutes": 5, "retry_limit": 1, "risk_level": "low",
            },
        ],
    }


def test_analysis_outputs_are_captured_as_a_dependency_patch(tmp_path):
    scenario = {
        "tasks": {
            "R001": {
                "write_files": [{"path": "docs/spec.md",
                                 "content": "# reference\npalette: #6b5a44\n"}],
                "summary": "reference written",
                "evidence": [{"file": "docs/spec.md", "lines": "1-2",
                              "description": "palette"}],
            },
            "R002": {"summary": "read reference"},
        },
    }
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    imported = hub.import_plan(_docs_plan(), main_agent_id="mock_main",
                               model="mock-model")
    for approval in hub.approvals_pending():
        if approval["kind"] == "architecture":
            hub.decide_approval(approval["approval_id"], approve=True)
    run_id = hub.approve_plan(imported["plan_id"])["run_id"]
    outcome = hub.start_run(run_id, max_parallel=1)
    assert outcome["status"] == "pending_acceptance", outcome

    research = next(t for t in hub.tasks(run_id=run_id) if t["task_id"] == "R001")
    assert research["status"] == "completed"
    # Analysis nodes are not writable, so the file was not part of any patch.
    assert not research["patch_path"]
    workdir = Path(research["workdir"])
    assert (workdir / "docs/spec.md").exists()

    captured = hub.capture_artifacts(research["uid"])
    assert captured["status"] == "captured"
    assert captured["files"] == ["docs/spec.md"]
    assert Path(captured["patch_path"]).exists()
    stored = next(t for t in hub.tasks(run_id=run_id) if t["task_id"] == "R001")
    assert stored["patch_path"] == captured["patch_path"]
    changes = hub.store.list_changes(task_uid=research["uid"])
    assert changes and changes[-1].files == ["docs/spec.md"]

    again = hub.capture_artifacts(research["uid"])
    assert again["status"] == "already_captured"
    assert again["files"] == ["docs/spec.md"]
    hub.close()


def test_capture_rejects_unfinished_or_missing_workdir(tmp_path):
    scenario = {"tasks": {"R001": {"sleep": 0.2, "summary": "done"}}}
    repo, hub, _ = make_project(tmp_path, scenario=scenario)
    imported = hub.import_plan(_docs_plan(), main_agent_id="mock_main",
                               model="mock-model")
    for approval in hub.approvals_pending():
        if approval["kind"] == "architecture":
            hub.decide_approval(approval["approval_id"], approve=True)
    run_id = hub.approve_plan(imported["plan_id"])["run_id"]
    research = next(t for t in hub.tasks(run_id=run_id) if t["task_id"] == "R001")
    assert research["status"] == "queued"
    try:
        hub.capture_artifacts(research["uid"])
    except Exception as exc:
        assert "已完成" in str(exc)
    else:  # pragma: no cover - the guard must raise
        raise AssertionError("capture must reject a queued task")
    hub.close()
