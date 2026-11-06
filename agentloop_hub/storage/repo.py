"""Typed repositories over the SQLite schema."""

from __future__ import annotations

import json
from typing import Any

from ..errors import NotFoundError, ValidationError
from ..models import (
    MEMORY_SYNC_LEVEL, AgentRecord, Approval, ChangeRecord, Checkpoint, ConversationRecord,
    DecisionRecord, MemoryRecord, Project, ResultPackage, Task, TaskPlan, TaskSpec, TaskStatus,
    UsageRecord, VerificationReport, assert_task_transition,
)
from ..util import hash_obj, new_id, now_iso
from .db import Database


def _j(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _summary_digest(summary: Any) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {}
    return {
        "title": summary.get("title", ""),
        "message_count": summary.get("message_count", 0),
        "user_requests": list(summary.get("user_requests", []))[:5],
        "last_assistant_reply": str(summary.get("last_assistant_reply", ""))[:300],
        "tool_names": list(summary.get("tool_names", []))[:20],
        "token_usage": summary.get("token_usage", {}),
    }


class Store:
    def __init__(self, db: Database, *, event_sink: Any | None = None):
        self.db = db
        self.event_sink = event_sink

    # ------------------------------------------------------------------
    # Context Bus sink
    # ------------------------------------------------------------------
    def emit(self, kind: str, payload: dict[str, Any], *, source: str = "control-plane",
             correlation_id: str = "", topic: str = "", sync_level: int | None = None) -> None:
        """Publish a domain event to the Context Bus when one is attached.

        The store is the single write path for task/run/memory/approval state,
        so hooking here guarantees that every state change is observable on the
        bus without duplicating publish calls in each service.
        """
        sink = self.event_sink
        if sink is None:
            return
        try:
            sink.emit(kind, payload, source=source, correlation_id=correlation_id,
                      topic=topic, sync_level=sync_level)
        except Exception:
            # Bus persistence must never corrupt the control plane transaction.
            pass

    # ------------------------------------------------------------------
    # Project
    # ------------------------------------------------------------------
    def save_project(self, project: Project) -> None:
        self.db.execute(
            """
            INSERT INTO projects(project_id, name, root, workspace_id, repository_id, worktree_id,
                git_remote, branch, commit_hash, tech_stack, build_command, test_command,
                status, created_at, updated_at, extra)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(project_id) DO UPDATE SET
                name=excluded.name, root=excluded.root, workspace_id=excluded.workspace_id,
                repository_id=excluded.repository_id, worktree_id=excluded.worktree_id,
                git_remote=excluded.git_remote, branch=excluded.branch, commit_hash=excluded.commit_hash,
                tech_stack=excluded.tech_stack, build_command=excluded.build_command,
                test_command=excluded.test_command, status=excluded.status,
                updated_at=excluded.updated_at, extra=excluded.extra
            """,
            (
                project.project_id, project.name, project.root, project.workspace_id,
                project.repository_id, project.worktree_id, project.git_remote, project.branch,
                project.commit, _dumps(project.tech_stack), project.build_command,
                project.test_command, project.status, project.created_at, project.updated_at,
                _dumps(project.extra),
            ),
        )

    def get_project(self, project_id: str) -> Project | None:
        row = self.db.query_one("SELECT * FROM projects WHERE project_id=?", (project_id,))
        if not row:
            return None
        return Project(
            project_id=row["project_id"], name=row["name"], root=row["root"],
            workspace_id=row["workspace_id"], repository_id=row["repository_id"],
            worktree_id=row["worktree_id"], git_remote=row["git_remote"],
            branch=row["branch"], commit=row["commit_hash"],
            tech_stack=_j(row["tech_stack"], []), build_command=row["build_command"],
            test_command=row["test_command"], status=row["status"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            extra=_j(row["extra"], {}),
        )

    def update_project_git(self, project_id: str, *, branch: str, commit: str, git_remote: str = "") -> None:
        self.db.execute(
            "UPDATE projects SET branch=?, commit_hash=?, git_remote=COALESCE(NULLIF(?,''), git_remote), "
            "updated_at=? WHERE project_id=?",
            (branch, commit, git_remote, now_iso(), project_id),
        )
        self.emit("project_index", {
            "branch": branch, "commit": commit, "git_remote": git_remote,
        }, correlation_id=project_id, sync_level=3)

    def list_projects(self) -> list[Project]:
        rows = self.db.query("SELECT * FROM projects ORDER BY created_at DESC")
        return [self.get_project(r["project_id"]) for r in rows]  # type: ignore[list-item]

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------
    def upsert_agent(self, agent: AgentRecord) -> None:
        agent.validate()
        exists = self.get_agent(agent.agent_id)
        created = exists.created_at if exists else agent.created_at
        self.db.execute(
            """
            INSERT INTO agents(agent_id, project_id, name, product, edition, version, adapter_type,
                adapter_config, access_level, capabilities, models, permissions, status, last_seen,
                last_error, discovered, created_at, updated_at, extra)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(agent_id) DO UPDATE SET
                name=excluded.name, product=excluded.product, edition=excluded.edition,
                version=excluded.version, adapter_type=excluded.adapter_type,
                adapter_config=excluded.adapter_config, access_level=excluded.access_level,
                capabilities=excluded.capabilities, models=excluded.models,
                permissions=excluded.permissions, status=excluded.status,
                last_seen=excluded.last_seen, last_error=excluded.last_error,
                discovered=excluded.discovered, updated_at=excluded.updated_at,
                extra=excluded.extra
            """,
            (
                agent.agent_id, agent.project_id, agent.name, agent.product, agent.edition,
                agent.version, agent.adapter_type, _dumps(agent.adapter_config),
                agent.access_level, _dumps(agent.capabilities), _dumps(agent.models),
                _dumps(agent.permissions), agent.status, agent.last_seen, agent.last_error,
                1 if agent.discovered else 0, created, agent.updated_at, _dumps(agent.extra),
            ),
        )
        self.emit("agent_status", {
            "agent_id": agent.agent_id, "name": agent.name, "product": agent.product,
            "status": agent.status, "access_level": agent.access_level,
            "adapter_type": agent.adapter_type,
            "capabilities": [k for k, v in (agent.capabilities or {}).items() if v],
        }, source="discovery" if agent.discovered else "user",
            correlation_id=agent.agent_id, sync_level=0)

    def get_agent(self, agent_id: str) -> AgentRecord | None:
        row = self.db.query_one("SELECT * FROM agents WHERE agent_id=?", (agent_id,))
        if not row:
            return None
        return AgentRecord(
            agent_id=row["agent_id"], name=row["name"], product=row["product"],
            edition=row["edition"], project_id=row["project_id"], version=row["version"],
            adapter_type=row["adapter_type"],
            adapter_config=_j(row["adapter_config"], {}), access_level=row["access_level"],
            capabilities=_j(row["capabilities"], {}), models=_j(row["models"], []),
            permissions=_j(row["permissions"], {}), status=row["status"],
            last_seen=row["last_seen"], last_error=row["last_error"],
            discovered=bool(row["discovered"]), created_at=row["created_at"],
            updated_at=row["updated_at"], extra=_j(row["extra"], {}),
        )

    def list_agents(self) -> list[AgentRecord]:
        rows = self.db.query("SELECT * FROM agents ORDER BY name")
        return [self.get_agent(r["agent_id"]) for r in rows]  # type: ignore[list-item]

    def delete_agent(self, agent_id: str) -> None:
        self.db.execute("DELETE FROM agents WHERE agent_id=?", (agent_id,))
        self.delete_agent_history(agent_id)
        self.emit("agent_status", {"agent_id": agent_id, "status": "removed"},
                  source="user", correlation_id=agent_id)

    def set_agent_status(self, agent_id: str, status: str, error: str = "") -> None:
        self.db.execute(
            "UPDATE agents SET status=?, last_error=?, last_seen=?, updated_at=? WHERE agent_id=?",
            (status, error, now_iso(), now_iso(), agent_id),
        )
        self.emit("agent_status", {"agent_id": agent_id, "status": status,
                                   "error": error[:500]},
                  source=agent_id, correlation_id=agent_id)

    def set_agent_models(self, agent_id: str, models: list[str]) -> None:
        self.db.execute(
            "UPDATE agents SET models=?, updated_at=? WHERE agent_id=?",
            (_dumps(sorted(set(models))), now_iso(), agent_id),
        )

    def update_agent_extra(self, agent_id: str, extra: dict[str, Any]) -> None:
        self.db.execute(
            "UPDATE agents SET extra=?, updated_at=? WHERE agent_id=?",
            (_dumps(extra), now_iso(), agent_id),
        )
        self.emit("agent_status", {"agent_id": agent_id, "event": "agent_config_changed"},
                  source="user", correlation_id=agent_id)

    # ------------------------------------------------------------------
    # Agent version ledger (AgentMesh OS v3)
    # ------------------------------------------------------------------
    @staticmethod
    def agent_fingerprint(agent: AgentRecord) -> str:
        """Stable compatibility fingerprint.

        Deliberately excludes environment variables (they may contain secrets)
        and includes only integration-relevant fields.
        """
        cfg = agent.adapter_config or {}
        payload = {
            "version": agent.version,
            "adapter_type": agent.adapter_type,
            "capabilities": {k: bool(v) for k, v in sorted((agent.capabilities or {}).items())},
            # Model catalog changes (including user selections) are tracked in
            # the model table, not treated as an Agent software version change.
            "integration": {
                key: cfg.get(key) for key in (
                    "mode", "executable", "data_path", "session_reader",
                    "command", "args", "mcp_server", "config_path",
                ) if cfg.get(key)
            },
        }
        return hash_obj(payload)

    def record_agent_version(self, project_id: str, agent: AgentRecord) -> dict[str, Any]:
        fingerprint = self.agent_fingerprint(agent)
        latest = self.latest_agent_version(project_id, agent.agent_id)
        now = now_iso()
        if latest and latest["fingerprint"] == fingerprint:
            self.db.execute(
                "UPDATE agent_versions SET last_seen=? WHERE revision=?",
                (now, latest["revision"]),
            )
            latest["last_seen"] = now
            latest["changed"] = False
            return latest

        changes: dict[str, Any] = {"initial": latest is None}
        compat = "ok"
        if latest:
            old_caps = set(latest.get("capabilities") or {})
            new_caps = {k for k, v in (agent.capabilities or {}).items() if v}
            added = sorted(new_caps - old_caps)
            removed = sorted(old_caps - new_caps)
            old_models = set(latest.get("models") or [])
            new_models = set(agent.models or [])
            changes.update({
                "version_from": latest.get("version", ""),
                "version_to": agent.version,
                "adapter_from": latest.get("adapter_type", ""),
                "adapter_to": agent.adapter_type,
                "capabilities_added": added,
                "capabilities_removed": removed,
                "models_added": sorted(new_models - old_models),
                "models_removed": sorted(old_models - new_models),
            })
            if removed or latest.get("adapter_type") != agent.adapter_type:
                compat = "drift"
            elif latest.get("version") != agent.version:
                compat = "upgraded"

        cur = self.db.execute(
            "INSERT INTO agent_versions(project_id, agent_id, version, adapter_type, fingerprint, "
            "capabilities, models, changes, compat_status, first_seen, last_seen) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                project_id, agent.agent_id, agent.version, agent.adapter_type, fingerprint,
                _dumps({k: bool(v) for k, v in (agent.capabilities or {}).items() if v}),
                _dumps(sorted(set(agent.models or []))), _dumps(changes), compat, now, now,
            ),
        )
        revision = int(cur.lastrowid or 0)
        self.emit("agent_status", {
            "agent_id": agent.agent_id, "event": "version_recorded",
            "version": agent.version, "compat_status": compat, "changes": changes,
        }, source="discovery", correlation_id=agent.agent_id)
        return self.latest_agent_version(project_id, agent.agent_id) or {
            "revision": revision, "compat_status": compat, "changes": changes,
            "changed": latest is None,
        }

    def latest_agent_version(self, project_id: str, agent_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM agent_versions WHERE project_id=? AND agent_id=? "
            "ORDER BY revision DESC LIMIT 1",
            (project_id, agent_id),
        )
        return self._agent_version_row(row) if row else None

    def list_agent_versions(self, project_id: str, *, agent_id: str = "",
                            limit: int = 200) -> list[dict[str, Any]]:
        if agent_id:
            rows = self.db.query(
                "SELECT * FROM agent_versions WHERE project_id=? AND agent_id=? "
                "ORDER BY revision DESC LIMIT ?", (project_id, agent_id, int(limit)),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM agent_versions WHERE project_id=? ORDER BY revision DESC LIMIT ?",
                (project_id, int(limit)),
            )
        return [self._agent_version_row(r) for r in rows]

    @staticmethod
    def _agent_version_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "capabilities": _j(row["capabilities"], {}),
            "models": _j(row["models"], []),
            "changes": _j(row["changes"], {}),
            "compat_status": row.get("compat_status", "ok"),
        }

    def delete_agent_history(self, agent_id: str) -> None:
        self.db.execute("DELETE FROM agent_versions WHERE agent_id=?", (agent_id,))
        self.db.execute("DELETE FROM agent_models WHERE agent_id=?", (agent_id,))

    # ------------------------------------------------------------------
    # Agent model catalog
    # ------------------------------------------------------------------
    SOURCE_RANK = {"discovered": 0, "session": 1, "cli": 2, "registered": 3, "user": 4}

    def upsert_agent_model(self, project_id: str, agent_id: str, model: str, *,
                           source: str = "discovered", note: str = "") -> dict[str, Any]:
        name = str(model or "").strip()
        if not name:
            raise ValidationError("模型名称不能为空")
        if len(name) > 200:
            raise ValidationError("模型名称过长")
        existing = self.db.query_one(
            "SELECT * FROM agent_models WHERE project_id=? AND agent_id=? AND model=?",
            (project_id, agent_id, name),
        )
        now = now_iso()
        if existing:
            old_rank = self.SOURCE_RANK.get(existing["source"], 0)
            new_rank = self.SOURCE_RANK.get(source, 0)
            self.db.execute(
                "UPDATE agent_models SET source=?, note=?, updated_at=? "
                "WHERE project_id=? AND agent_id=? AND model=?",
                (source if new_rank >= old_rank else existing["source"],
                 note or existing["note"], now, project_id, agent_id, name),
            )
        else:
            self.db.execute(
                "INSERT INTO agent_models(project_id, agent_id, model, source, note, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                (project_id, agent_id, name, source, note, now, now),
            )
        return self.db.query_one(
            "SELECT * FROM agent_models WHERE project_id=? AND agent_id=? AND model=?",
            (project_id, agent_id, name),
        ) or {"project_id": project_id, "agent_id": agent_id, "model": name,
              "source": source, "note": note}

    def list_agent_models(self, project_id: str, *, agent_id: str = "") -> list[dict[str, Any]]:
        if agent_id:
            return self.db.query(
                "SELECT * FROM agent_models WHERE project_id=? AND agent_id=? ORDER BY model",
                (project_id, agent_id),
            )
        return self.db.query(
            "SELECT * FROM agent_models WHERE project_id=? ORDER BY agent_id, model",
            (project_id,),
        )

    def delete_agent_model(self, project_id: str, agent_id: str, model: str) -> None:
        self.db.execute(
            "DELETE FROM agent_models WHERE project_id=? AND agent_id=? AND model=?",
            (project_id, agent_id, model),
        )

    def replace_discovered_models(self, project_id: str, agent_id: str,
                                  models: list[str], *, source: str = "discovered") -> None:
        """Refresh machine-detected models while preserving user registrations."""
        discovered_sources = {"discovered", "session", "cli", "sync"}
        marks = ",".join("?" for _ in discovered_sources)
        self.db.execute(
            f"DELETE FROM agent_models WHERE project_id=? AND agent_id=? "
            f"AND source IN ({marks})",
            (project_id, agent_id, *sorted(discovered_sources)),
        )
        for name in sorted(set(models)):
            self.upsert_agent_model(project_id, agent_id, name, source=source)

    # ------------------------------------------------------------------
    # Main node
    # ------------------------------------------------------------------
    def set_main_node(self, project_id: str, agent_id: str, model: str = "",
                      budget_tokens: int = 0, permissions: dict | None = None,
                      note: str = "") -> int:
        with self.db.transaction() as conn:
            conn.execute("UPDATE main_nodes SET active=0 WHERE project_id=? AND active=1", (project_id,))
            cur = conn.execute(
                "INSERT INTO main_nodes(project_id, agent_id, model, scope, budget_tokens, permissions, active, set_at, note) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (project_id, agent_id, model, "project", int(budget_tokens), _dumps(permissions or {}), 1, now_iso(), note),
            )
            return int(cur.lastrowid)

    def get_main_node(self, project_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM main_nodes WHERE project_id=? AND active=1 ORDER BY revision DESC LIMIT 1",
            (project_id,),
        )
        if not row:
            return None
        row["permissions"] = _j(row["permissions"], {})
        return row

    def main_node_history(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM main_nodes WHERE project_id=? ORDER BY revision DESC", (project_id,))
        for r in rows:
            r["permissions"] = _j(r["permissions"], {})
        return rows

    # ------------------------------------------------------------------
    # Plans
    # ------------------------------------------------------------------
    def save_plan(self, plan: TaskPlan) -> None:
        self.db.execute(
            """
            INSERT INTO plans(plan_id, project_id, goal, constraints, tasks, main_agent_id, model,
                status, raw, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(plan_id) DO UPDATE SET
                goal=excluded.goal, constraints=excluded.constraints, tasks=excluded.tasks,
                main_agent_id=excluded.main_agent_id, model=excluded.model,
                status=excluded.status, raw=excluded.raw
            """,
            (
                plan.plan_id, plan.project_id, plan.goal, _dumps(plan.constraints),
                _dumps([t.to_dict() for t in plan.tasks]), plan.main_agent_id, plan.model,
                plan.status, _dumps(plan.raw), plan.created_at,
            ),
        )

    def get_plan(self, plan_id: str) -> TaskPlan | None:
        row = self.db.query_one("SELECT * FROM plans WHERE plan_id=?", (plan_id,))
        if not row:
            return None
        return self._plan_from_row(row)

    def _plan_from_row(self, row: dict[str, Any]) -> TaskPlan:
        tasks = [TaskSpec.from_dict(t) for t in _j(row["tasks"], [])]
        return TaskPlan(
            plan_id=row["plan_id"], project_id=row["project_id"], goal=row["goal"],
            tasks=tasks, constraints=_j(row["constraints"], []),
            main_agent_id=row["main_agent_id"], model=row["model"], status=row["status"],
            created_at=row["created_at"], raw=_j(row["raw"], {}),
        )

    def list_plans(self, project_id: str, limit: int = 50) -> list[TaskPlan]:
        rows = self.db.query(
            "SELECT * FROM plans WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
        )
        return [self._plan_from_row(r) for r in rows]

    def update_plan_status(self, plan_id: str, status: str, *, approved_at: str = "",
                           approval_id: str = "") -> None:
        self.db.execute(
            "UPDATE plans SET status=?, approved_at=?, approval_id=? WHERE plan_id=?",
            (status, approved_at, approval_id, plan_id),
        )

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------
    def create_run(self, project_id: str, plan_id: str, *, main_agent: str, model: str,
                   base_commit: str, base_branch: str, status: str = "ready",
                   round_number: int = 1, max_rounds: int = 0,
                   goal_status: str = "active") -> str:
        run_id = new_id("run")
        self.db.execute(
            "INSERT INTO runs(run_id, project_id, plan_id, status, main_agent_id, model, "
            "base_commit, base_branch, round_number, max_rounds, goal_status, "
            "created_at, started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, project_id, plan_id, status, main_agent, model, base_commit, base_branch,
             int(round_number), int(max_rounds), goal_status,
             now_iso(), now_iso() if status == "running" else ""),
        )
        self.emit("workflow", {"run_id": run_id, "plan_id": plan_id, "status": status,
                               "main_agent_id": main_agent, "model": model,
                               "round_number": int(round_number),
                               "max_rounds": int(max_rounds)},
                  correlation_id=run_id)
        return run_id

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        if row:
            row["summary"] = _j(row["summary"], {})
        return row

    def list_runs(self, project_id: str, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM runs WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
                             (project_id, limit))
        for r in rows:
            r["summary"] = _j(r["summary"], {})
        return rows

    def latest_run(self, project_id: str) -> dict[str, Any] | None:
        rows = self.list_runs(project_id, 1)
        return rows[0] if rows else None

    def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = {
            "status", "summary", "error", "started_at", "finished_at", "main_agent_id", "model",
            "acceptance_status", "release_status", "acceptance_at", "release_at",
            "round_number", "max_rounds", "goal_status", "blocked_reason",
        }
        assignments = []
        values = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValidationError(f"不允许更新 run 字段 {key}")
            assignments.append(f"{key}=?")
            values.append(_dumps(value) if key == "summary" and not isinstance(value, str) else value)
        if not assignments:
            return
        values.append(run_id)
        self.db.execute(f"UPDATE runs SET {', '.join(assignments)} WHERE run_id=?", values)
        if ("status" in fields or "acceptance_status" in fields or "release_status" in fields
                or "round_number" in fields or "goal_status" in fields):
            self.emit("workflow", {
                "run_id": run_id,
                "status": fields.get("status"),
                "acceptance_status": fields.get("acceptance_status"),
                "release_status": fields.get("release_status"),
                "round_number": fields.get("round_number"),
                "max_rounds": fields.get("max_rounds"),
                "goal_status": fields.get("goal_status"),
                "blocked_reason": fields.get("blocked_reason"),
            }, correlation_id=run_id)

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
    def create_tasks(self, run_id: str, plan: TaskPlan, base_commit: str,
                     specs: list[TaskSpec] | None = None) -> list[Task]:
        tasks: list[Task] = []
        now = now_iso()
        with self.db.transaction() as conn:
            for spec in (specs if specs is not None else plan.topological_order()):
                uid = f"{run_id}:{spec.task_id}"
                conn.execute(
                    """
                    INSERT INTO tasks(uid, project_id, plan_id, run_id, task_id, parent_task_id, title,
                        objective, type, role, model, suggested_agent, assigned_agent,
                        scope, out_of_scope, dependencies,
                        required_capabilities,
                        input_artifacts, expected_outputs, acceptance_criteria, verification_commands,
                        token_budget, time_limit_minutes, retry_limit, risk_level, approval_policy,
                        priority, context_length, capability_level,
                        status, created_at, updated_at, base_commit)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        uid, plan.project_id, plan.plan_id, run_id, spec.task_id, spec.parent_task_id,
                        spec.title, spec.objective, spec.type, spec.role, spec.model,
                        spec.suggested_agent,
                        # A pinned model is an explicit chain binding; show the
                        # effective agent immediately. Unpinned tasks stay blank
                        # so the capability router can still choose.
                        (spec.suggested_agent if spec.model else ""),
                        _dumps(spec.scope),
                        _dumps(spec.out_of_scope), _dumps(spec.dependencies),
                        _dumps(spec.required_capabilities), _dumps(spec.input_artifacts),
                        _dumps(spec.expected_outputs), _dumps(spec.acceptance_criteria),
                        _dumps(spec.verification_commands), int(spec.token_budget),
                        int(spec.time_limit_minutes), int(spec.retry_limit), spec.risk_level,
                        spec.approval_policy, int(spec.priority),
                        int(spec.context_length or 0), spec.capability_level or "",
                        TaskStatus.QUEUED.value,
                        now, now, base_commit,
                    ),
                )
                tasks.append(Task(
                    uid=uid, project_id=plan.project_id, plan_id=plan.plan_id, run_id=run_id,
                    spec=spec, status=TaskStatus.QUEUED.value, base_commit=base_commit,
                    created_at=now, updated_at=now,
                ))
        return tasks

    def _task_from_row(self, row: dict[str, Any]) -> Task:
        spec = TaskSpec(
            task_id=row["task_id"], title=row["title"], objective=row["objective"],
            type=row["type"], role=str(row.get("role") or ""), model=str(row.get("model") or ""),
            suggested_agent=row["suggested_agent"], parent_task_id=row["parent_task_id"],
            scope=_j(row["scope"], []), out_of_scope=_j(row["out_of_scope"], []),
            dependencies=_j(row["dependencies"], []),
            required_capabilities=_j(row["required_capabilities"], []),
            input_artifacts=_j(row["input_artifacts"], []),
            expected_outputs=_j(row["expected_outputs"], []),
            acceptance_criteria=_j(row["acceptance_criteria"], []),
            verification_commands=_j(row["verification_commands"], []),
            token_budget=int(row["token_budget"]), time_limit_minutes=int(row["time_limit_minutes"]),
            retry_limit=int(row["retry_limit"]), risk_level=row["risk_level"],
            approval_policy=row["approval_policy"], priority=int(row.get("priority", 5) or 5),
            context_length=int(row.get("context_length", 0) or 0),
            capability_level=str(row.get("capability_level") or ""),
        )
        return Task(
            uid=row["uid"], project_id=row["project_id"], plan_id=row["plan_id"],
            run_id=row["run_id"], spec=spec, status=row["status"],
            assigned_agent=row["assigned_agent"], attempts=int(row["attempts"]),
            workdir=row["workdir"], task_dir=row["task_dir"], base_commit=row["base_commit"],
            patch_path=row["patch_path"], result_path=row["result_path"],
            verification_status=row["verification_status"], error=row["error"],
            pid=int(row["pid"]), created_at=row["created_at"], updated_at=row["updated_at"],
            started_at=row["started_at"], finished_at=row["finished_at"],
        )

    def get_task(self, uid: str) -> Task | None:
        row = self.db.query_one("SELECT * FROM tasks WHERE uid=?", (uid,))
        return self._task_from_row(row) if row else None

    def get_task_by_display(self, run_id: str, task_id: str) -> Task | None:
        row = self.db.query_one("SELECT * FROM tasks WHERE run_id=? AND task_id=?", (run_id, task_id))
        return self._task_from_row(row) if row else None

    def list_tasks(self, run_id: str) -> list[Task]:
        rows = self.db.query("SELECT * FROM tasks WHERE run_id=? ORDER BY created_at, task_id", (run_id,))
        return [self._task_from_row(r) for r in rows]

    def list_tasks_by_project(self, project_id: str, limit: int = 200) -> list[Task]:
        rows = self.db.query(
            "SELECT * FROM tasks WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
        )
        return [self._task_from_row(r) for r in rows]

    def update_task(self, uid: str, **fields: Any) -> None:
        allowed = {
            "assigned_agent", "attempts", "workdir", "task_dir", "base_commit", "patch_path",
            "result_path", "verification_status", "error", "pid", "started_at", "finished_at",
            "role", "model", "suggested_agent", "context_length", "capability_level",
        }
        assignments = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValidationError(f"不允许更新 task 字段 {key}")
            assignments.append(f"{key}=?")
            values.append(value)
        if not assignments:
            return
        assignments.append("updated_at=?")
        values.append(now_iso())
        values.append(uid)
        self.db.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE uid=?", values)

    def set_task_status(self, uid: str, status: str, *, error: str = "", force: bool = False) -> None:
        task = self.get_task(uid)
        if not task:
            raise NotFoundError(f"任务不存在: {uid}")
        if not force:
            assert_task_transition(task.status, status)
        fields: dict[str, Any] = {"status": status}
        if status in {
            TaskStatus.COMPLETED.value, TaskStatus.QUEUED.value,
            TaskStatus.RUNNING.value, TaskStatus.VERIFYING.value,
        }:
            # A new attempt or a completed node must not keep the previous
            # attempt's stall/block message in the UI.
            fields["error"] = error or ""
        elif error:
            fields["error"] = error
        if status == TaskStatus.RUNNING.value and not task.started_at:
            fields["started_at"] = now_iso()
        if status in {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.REJECTED.value,
                      TaskStatus.CANCELED.value}:
            fields["finished_at"] = now_iso()
        elif status in {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value,
                        TaskStatus.VERIFYING.value, TaskStatus.WAITING_USER.value}:
            # A retried/running node must not keep the previous attempt's
            # finished_at, which made the UI show "running" with an end time.
            fields["finished_at"] = ""
        assignments = [f"{k}=?" for k in fields]
        values = list(fields.values())
        assignments.append("updated_at=?")
        values.append(now_iso())
        values.append(uid)
        self.db.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE uid=?", values)
        self.emit("task_state", {
            "task_uid": uid, "task_id": task.task_id, "status": status,
            "assigned_agent": task.assigned_agent, "attempts": task.attempts,
            "verification_status": task.verification_status, "error": error[:500],
        }, source="control-plane", correlation_id=uid, sync_level=2)

    # ------------------------------------------------------------------
    # Results / verifications
    # ------------------------------------------------------------------
    def save_result(self, task: Task, result: ResultPackage, *, valid: bool, error: str = "") -> None:
        self.db.execute(
            """
            INSERT INTO results(task_uid, project_id, task_id, agent_id, status, payload, valid,
                validation_error, received_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(task_uid) DO UPDATE SET
                agent_id=excluded.agent_id, status=excluded.status, payload=excluded.payload,
                valid=excluded.valid, validation_error=excluded.validation_error,
                received_at=excluded.received_at
            """,
            (task.uid, task.project_id, task.task_id, str(result.payload.get("agent_id", "")),
             result.status, _dumps(result.payload), 1 if valid else 0, error, now_iso()),
        )
        self.emit("response", {
            "task_uid": task.uid, "task_id": task.task_id,
            "agent_id": str(result.payload.get("agent_id", "")),
            "status": result.status, "valid": valid,
            "summary": result.summary[:500],
            "files_changed": result.files_changed[:50],
            "token_usage": result.token_usage,
            "validation_error": error[:500],
        }, source=str(result.payload.get("agent_id", "") or "agent"),
            correlation_id=task.uid)

    def get_result(self, uid: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM results WHERE task_uid=?", (uid,))
        if not row:
            return None
        row["payload"] = _j(row["payload"], {})
        row["valid"] = bool(row["valid"])
        return row

    def add_verification(self, report: VerificationReport, *, project_id: str,
                         task_uid: str, report_path: str = "") -> str:
        vid = new_id("ver")
        self.db.execute(
            "INSERT INTO verifications(verification_id, project_id, task_uid, task_id, verifier_agent, "
            "status, checks, warnings, summary, report_path, source, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (vid, project_id, task_uid, report.task_id, report.verifier_agent, report.status,
             _dumps(report.checks), _dumps(report.warnings), report.summary, report_path,
             report.source, report.created_at),
        )
        self.emit("verification", {
            "verification_id": vid, "task_uid": task_uid, "task_id": report.task_id,
            "verifier_agent": report.verifier_agent, "status": report.status,
            "checks": report.checks, "warnings": report.warnings[:20],
        }, source=report.verifier_agent, correlation_id=task_uid)
        return vid

    def latest_verification(self, uid: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM verifications WHERE task_uid=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (uid,),
        )
        if not row:
            return None
        row["checks"] = _j(row["checks"], {})
        row["warnings"] = _j(row["warnings"], [])
        return row

    def list_verifications(self, uid: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM verifications WHERE task_uid=? ORDER BY created_at", (uid,))
        for r in rows:
            r["checks"] = _j(r["checks"], {})
            r["warnings"] = _j(r["warnings"], [])
        return rows

    # ------------------------------------------------------------------
    # Changes / leases
    # ------------------------------------------------------------------
    def add_change(self, change: ChangeRecord) -> None:
        self.db.execute(
            "INSERT INTO changes(change_id, project_id, task_uid, agent_id, base_commit, base_hash, "
            "before_hash, after_hash, files, patch_path, status, created_at, applied_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (change.change_id, change.project_id, change.task_uid, change.agent_id,
             change.base_commit, change.base_hash, change.before_hash, change.after_hash,
             _dumps(change.files), change.patch_path, change.status, change.created_at,
             change.applied_at),
        )
        self.emit("file_change", {
            "change_id": change.change_id, "task_uid": change.task_uid,
            "agent_id": change.agent_id, "files": change.files,
            "patch_path": change.patch_path, "before_hash": change.before_hash,
            "after_hash": change.after_hash, "status": change.status,
        }, source=change.agent_id or "control-plane", correlation_id=change.task_uid)

    def get_change(self, change_id: str) -> ChangeRecord | None:
        row = self.db.query_one("SELECT * FROM changes WHERE change_id=?", (change_id,))
        return self._change_from_row(row) if row else None

    def _change_from_row(self, row: dict[str, Any]) -> ChangeRecord:
        return ChangeRecord(
            change_id=row["change_id"], project_id=row["project_id"], task_uid=row["task_uid"],
            agent_id=row["agent_id"], base_commit=row["base_commit"], base_hash=row["base_hash"],
            before_hash=row["before_hash"], after_hash=row["after_hash"],
            files=_j(row["files"], []), patch_path=row["patch_path"], status=row["status"],
            created_at=row["created_at"], applied_at=row["applied_at"],
        )

    def list_changes(self, *, task_uid: str = "", run_id: str = "", project_id: str = "") -> list[ChangeRecord]:
        clauses = []
        params: list[Any] = []
        if task_uid:
            clauses.append("c.task_uid=?")
            params.append(task_uid)
        if run_id:
            clauses.append("t.run_id=?")
            params.append(run_id)
        if project_id:
            clauses.append("c.project_id=?")
            params.append(project_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.db.query(
            f"SELECT c.* FROM changes c LEFT JOIN tasks t ON t.uid=c.task_uid{where} "
            "ORDER BY c.created_at",
            params,
        )
        return [self._change_from_row(r) for r in rows]

    def update_change_status(self, change_id: str, status: str, *, applied_at: str = "") -> None:
        self.db.execute(
            "UPDATE changes SET status=?, applied_at=? WHERE change_id=?",
            (status, applied_at, change_id),
        )

    def acquire_lease(self, project_id: str, pattern: str, task_uid: str, agent_id: str,
                      mode: str = "write") -> str:
        lease_id = new_id("lease")
        self.db.execute(
            "INSERT INTO file_leases(lease_id, project_id, path_pattern, task_uid, agent_id, mode, "
            "active, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (lease_id, project_id, pattern, task_uid, agent_id, mode, 1, now_iso()),
        )
        return lease_id

    def active_leases(self, project_id: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM file_leases WHERE project_id=? AND active=1 ORDER BY created_at",
            (project_id,),
        )

    def release_task_leases(self, task_uid: str) -> None:
        self.db.execute(
            "UPDATE file_leases SET active=0, released_at=? WHERE task_uid=? AND active=1",
            (now_iso(), task_uid),
        )

    def release_all_leases(self, project_id: str) -> None:
        self.db.execute(
            "UPDATE file_leases SET active=0, released_at=? WHERE project_id=? AND active=1",
            (now_iso(), project_id),
        )

    # ------------------------------------------------------------------
    # Approvals
    # ------------------------------------------------------------------
    def create_approval(self, approval: Approval) -> None:
        self.db.execute(
            "INSERT INTO approvals(approval_id, project_id, kind, subject, details, status, plan_id, "
            "run_id, task_uid, requested_at, decided_at, decided_by, note) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (approval.approval_id, approval.project_id, approval.kind, approval.subject,
             _dumps(approval.details), approval.status, approval.plan_id, approval.run_id,
             approval.task_uid, approval.requested_at, approval.decided_at, approval.decided_by,
             approval.note),
        )
        self.emit("governance", {
            "approval_id": approval.approval_id, "kind": approval.kind,
            "subject": approval.subject, "status": approval.status,
            "plan_id": approval.plan_id, "run_id": approval.run_id,
            "task_uid": approval.task_uid,
        }, source="control-plane", correlation_id=approval.run_id or approval.plan_id)

    def get_approval(self, approval_id: str) -> Approval | None:
        row = self.db.query_one("SELECT * FROM approvals WHERE approval_id=?", (approval_id,))
        if not row:
            return None
        return Approval(
            approval_id=row["approval_id"], project_id=row["project_id"], kind=row["kind"],
            subject=row["subject"], details=_j(row["details"], {}), status=row["status"],
            plan_id=row["plan_id"], run_id=row["run_id"], task_uid=row["task_uid"],
            requested_at=row["requested_at"], decided_at=row["decided_at"],
            decided_by=row["decided_by"], note=row["note"],
        )

    def list_approvals(self, project_id: str, *, status: str = "", limit: int = 100) -> list[Approval]:
        if status:
            rows = self.db.query(
                "SELECT * FROM approvals WHERE project_id=? AND status=? ORDER BY requested_at DESC LIMIT ?",
                (project_id, status, limit),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM approvals WHERE project_id=? ORDER BY requested_at DESC LIMIT ?",
                (project_id, limit),
            )
        return [self.get_approval(r["approval_id"]) for r in rows]  # type: ignore[list-item]

    def decide_approval(self, approval_id: str, status: str, *, by: str, note: str = "") -> Approval:
        if status not in ("approved", "rejected"):
            raise ValidationError("审批结果必须是 approved/rejected")
        self.db.execute(
            "UPDATE approvals SET status=?, decided_at=?, decided_by=?, note=? WHERE approval_id=?",
            (status, now_iso(), by, note, approval_id),
        )
        approval = self.get_approval(approval_id)
        if not approval:
            raise NotFoundError(f"审批不存在: {approval_id}")
        self.emit("governance", {
            "approval_id": approval.approval_id, "kind": approval.kind,
            "subject": approval.subject, "status": status, "decided_by": by,
            "plan_id": approval.plan_id, "run_id": approval.run_id,
            "task_uid": approval.task_uid,
        }, source=by, correlation_id=approval.run_id or approval.plan_id)
        return approval

    def find_pending_approval(self, project_id: str, *, kind: str, subject: str = "",
                              plan_id: str = "", run_id: str = "") -> Approval | None:
        clauses = ["project_id=?", "kind=?", "status='pending'"]
        params: list[Any] = [project_id, kind]
        if subject:
            clauses.append("subject=?")
            params.append(subject)
        if plan_id:
            clauses.append("plan_id=?")
            params.append(plan_id)
        if run_id:
            clauses.append("run_id=?")
            params.append(run_id)
        row = self.db.query_one(
            f"SELECT approval_id FROM approvals WHERE {' AND '.join(clauses)} ORDER BY requested_at DESC LIMIT 1",
            params,
        )
        return self.get_approval(row["approval_id"]) if row else None

    # ------------------------------------------------------------------
    # Usage
    # ------------------------------------------------------------------
    def add_usage(self, usage: UsageRecord) -> None:
        total = usage.total_tokens or (usage.input_tokens + usage.output_tokens)
        self.db.execute(
            "INSERT INTO usage_records(record_id, project_id, task_uid, run_id, agent_id, model, "
            "input_tokens, output_tokens, cached_tokens, reasoning_tokens, total_tokens, tool_calls, "
            "retries, duration_ms, cost, estimated, status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (usage.record_id, usage.project_id, usage.task_uid, usage.run_id, usage.agent_id,
             usage.model, usage.input_tokens, usage.output_tokens, usage.cached_tokens,
             usage.reasoning_tokens, total, usage.tool_calls, usage.retries, usage.duration_ms,
             float(usage.cost), 1 if usage.estimated else 0, usage.status, usage.created_at),
        )

    def usage_totals(self, project_id: str, *, run_id: str = "", task_uid: str = "") -> dict[str, Any]:
        clauses = ["project_id=?"]
        params: list[Any] = [project_id]
        if run_id:
            clauses.append("run_id=?")
            params.append(run_id)
        if task_uid:
            clauses.append("task_uid=?")
            params.append(task_uid)
        row = self.db.query_one(
            "SELECT COUNT(*) AS records, COALESCE(SUM(input_tokens),0) AS input_tokens, "
            "COALESCE(SUM(output_tokens),0) AS output_tokens, COALESCE(SUM(cached_tokens),0) AS cached_tokens, "
            "COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens, COALESCE(SUM(total_tokens),0) AS total_tokens, "
            "COALESCE(SUM(cost),0) AS cost, COALESCE(SUM(tool_calls),0) AS tool_calls, "
            "COALESCE(SUM(retries),0) AS retries, COALESCE(SUM(duration_ms),0) AS duration_ms "
            f"FROM usage_records WHERE {' AND '.join(clauses)}",
            params,
        )
        return row or {}

    def usage_by_agent(self, project_id: str, *, run_id: str = "") -> list[dict[str, Any]]:
        where = "project_id=?" + (" AND run_id=?" if run_id else "")
        params: list[Any] = [project_id] + ([run_id] if run_id else [])
        return self.db.query(
            "SELECT agent_id, model, COUNT(*) AS records, "
            "COALESCE(SUM(input_tokens),0) AS input_tokens, "
            "COALESCE(SUM(output_tokens),0) AS output_tokens, "
            "COALESCE(SUM(cached_tokens),0) AS cached_tokens, "
            "COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens, "
            "COALESCE(SUM(total_tokens),0) AS total_tokens, "
            "COALESCE(SUM(cost),0) AS cost, COALESCE(SUM(retries),0) AS retries, "
            "COALESCE(SUM(duration_ms),0) AS duration_ms "
            f"FROM usage_records WHERE {where} GROUP BY agent_id, model ORDER BY total_tokens DESC",
            params,
        )

    def usage_by_task(self, project_id: str, *, run_id: str = "") -> list[dict[str, Any]]:
        where = "project_id=?" + (" AND run_id=?" if run_id else "")
        params: list[Any] = [project_id] + ([run_id] if run_id else [])
        return self.db.query(
            "SELECT task_uid, agent_id, COALESCE(SUM(total_tokens),0) AS total_tokens, "
            "COALESCE(SUM(cost),0) AS cost, COALESCE(SUM(retries),0) AS retries "
            f"FROM usage_records WHERE {where} GROUP BY task_uid, agent_id ORDER BY total_tokens DESC",
            params,
        )

    def list_usage(self, project_id: str, *, limit: int = 500) -> list[UsageRecord]:
        rows = self.db.query(
            "SELECT * FROM usage_records WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
        )
        return [
            UsageRecord(
                record_id=r["record_id"], project_id=r["project_id"], agent_id=r["agent_id"],
                model=r["model"], task_uid=r["task_uid"], run_id=r["run_id"],
                input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
                cached_tokens=r["cached_tokens"], reasoning_tokens=r["reasoning_tokens"],
                total_tokens=r["total_tokens"], tool_calls=r["tool_calls"],
                retries=r["retries"], duration_ms=r["duration_ms"], cost=r["cost"],
                estimated=bool(r["estimated"]), status=r["status"], created_at=r["created_at"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Checkpoints / audit
    # ------------------------------------------------------------------
    def add_checkpoint(self, checkpoint: Checkpoint) -> None:
        self.db.execute(
            "INSERT INTO checkpoints(checkpoint_id, project_id, run_id, task_uid, kind, snapshot, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (checkpoint.checkpoint_id, checkpoint.project_id, checkpoint.run_id, checkpoint.task_uid,
             checkpoint.kind, _dumps(checkpoint.snapshot), checkpoint.created_at),
        )

    def latest_checkpoint(self, project_id: str, *, run_id: str = "") -> dict[str, Any] | None:
        if run_id:
            row = self.db.query_one(
                "SELECT * FROM checkpoints WHERE project_id=? AND run_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (project_id, run_id),
            )
        else:
            row = self.db.query_one(
                "SELECT * FROM checkpoints WHERE project_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (project_id,),
            )
        if row:
            row["snapshot"] = _j(row["snapshot"], {})
        return row

    def list_checkpoints(self, project_id: str, *, run_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if run_id:
            rows = self.db.query(
                "SELECT * FROM checkpoints WHERE project_id=? AND run_id=? ORDER BY created_at DESC LIMIT ?",
                (project_id, run_id, limit),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM checkpoints WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            )
        for r in rows:
            r["snapshot"] = _j(r["snapshot"], {})
        return rows

    def append_audit(self, project_id: str, actor: str, action: str, subject: str = "",
                     payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        with self.db.transaction() as conn:
            prev = conn.execute(
                "SELECT hash FROM audit WHERE project_id=? ORDER BY seq DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            prev_hash = prev["hash"] if prev else ""
            ts = now_iso()
            base = {
                "project_id": project_id, "ts": ts, "actor": actor, "action": action,
                "subject": subject, "payload": payload, "prev_hash": prev_hash,
            }
            digest = hash_obj(base)
            conn.execute(
                "INSERT INTO audit(project_id, ts, actor, action, subject, payload, prev_hash, hash) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (project_id, ts, actor, action, subject, _dumps(payload), prev_hash, digest),
            )
            row = conn.execute(
                "SELECT * FROM audit WHERE project_id=? ORDER BY seq DESC LIMIT 1", (project_id,)
            ).fetchone()
            out = dict(row)
            out["payload"] = payload
            return out

    def list_audit(self, project_id: str, *, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM audit WHERE project_id=? ORDER BY seq ASC LIMIT ? OFFSET ?",
            (project_id, limit, offset),
        )
        for r in rows:
            r["payload"] = _j(r["payload"], {})
        return rows

    def audit_count(self, project_id: str) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM audit WHERE project_id=?", (project_id,))
        return int(row["n"]) if row else 0

    def verify_audit_chain(self, project_id: str) -> tuple[bool, list[str]]:
        rows = self.db.query("SELECT * FROM audit WHERE project_id=? ORDER BY seq ASC", (project_id,))
        prev = ""
        errors: list[str] = []
        for row in rows:
            base = {
                "project_id": row["project_id"], "ts": row["ts"], "actor": row["actor"],
                "action": row["action"], "subject": row["subject"],
                "payload": _j(row["payload"], {}), "prev_hash": prev,
            }
            expected = hash_obj(base)
            if row["prev_hash"] != prev:
                errors.append(f"seq {row['seq']}: prev_hash 不匹配")
            if row["hash"] != expected:
                errors.append(f"seq {row['seq']}: hash 不匹配")
            prev = row["hash"]
        return (not errors, errors)

    # ------------------------------------------------------------------
    # Memories
    # ------------------------------------------------------------------
    def add_memory(self, memory: MemoryRecord) -> MemoryRecord:
        memory.validate()
        # Conflict detection: same type+title, different body, both active.
        existing = self.db.query_one(
            "SELECT * FROM memories WHERE project_id=? AND type=? AND title=? AND status='active' "
            "AND body<>? LIMIT 1",
            (memory.project_id, memory.type, memory.title, memory.body),
        )
        if existing:
            group = existing["conflict_group"] or new_id("memconflict")
            self.db.execute(
                "UPDATE memories SET status='conflict', conflict_group=?, updated_at=? WHERE memory_id=?",
                (group, now_iso(), existing["memory_id"]),
            )
            memory.status = "conflict"
            memory.conflict_group = group
        self.db.execute(
            """
            INSERT INTO memories(memory_id, project_id, type, title, body, source, confidence, scope,
                task_uid, expires_at, invalid_conditions, last_verified_at, injectable, status,
                conflict_group, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (memory.memory_id, memory.project_id, memory.type, memory.title, memory.body,
             memory.source, float(memory.confidence), memory.scope, memory.task_uid,
             memory.expires_at, _dumps(memory.invalid_conditions), memory.last_verified_at,
             1 if memory.injectable else 0, memory.status, memory.conflict_group,
             memory.created_at, memory.updated_at),
        )
        self.emit("knowledge", {
            "memory_id": memory.memory_id, "type": memory.type, "title": memory.title,
            "status": memory.status, "confidence": memory.confidence,
            "body": memory.body[:500],
        }, source=memory.source or "memory", correlation_id=memory.memory_id,
            sync_level=MEMORY_SYNC_LEVEL.get(memory.type))
        return memory

    def get_memory(self, memory_id: str) -> MemoryRecord | None:
        row = self.db.query_one("SELECT * FROM memories WHERE memory_id=?", (memory_id,))
        return self._memory_from_row(row) if row else None

    def _memory_from_row(self, row: dict[str, Any]) -> MemoryRecord:
        return MemoryRecord(
            memory_id=row["memory_id"], project_id=row["project_id"], type=row["type"],
            title=row["title"], body=row["body"], source=row["source"],
            confidence=float(row["confidence"]), scope=row["scope"], task_uid=row["task_uid"],
            expires_at=row["expires_at"], invalid_conditions=_j(row["invalid_conditions"], []),
            last_verified_at=row["last_verified_at"], injectable=bool(row["injectable"]),
            status=row["status"], conflict_group=row["conflict_group"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def list_memories(self, project_id: str, *, type: str = "", status: str = "",
                      limit: int = 500) -> list[MemoryRecord]:
        clauses = ["project_id=?"]
        params: list[Any] = [project_id]
        if type:
            clauses.append("type=?")
            params.append(type)
        if status:
            clauses.append("status=?")
            params.append(status)
        rows = self.db.query(
            f"SELECT * FROM memories WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?",
            params + [limit],
        )
        return [self._memory_from_row(r) for r in rows]

    def update_memory(self, memory_id: str, **fields: Any) -> None:
        allowed = {"title", "body", "source", "confidence", "scope", "expires_at",
                   "invalid_conditions", "last_verified_at", "injectable", "status", "conflict_group"}
        assignments = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValidationError(f"不允许更新 memory 字段 {key}")
            assignments.append(f"{key}=?")
            if key == "invalid_conditions":
                value = _dumps(value)
            elif key == "injectable":
                value = 1 if value else 0
            values.append(value)
        if not assignments:
            return
        assignments.append("updated_at=?")
        values.append(now_iso())
        values.append(memory_id)
        self.db.execute(f"UPDATE memories SET {', '.join(assignments)} WHERE memory_id=?", values)
        memory = self.get_memory(memory_id)
        if memory:
            self.emit("knowledge", {
                "memory_id": memory.memory_id, "type": memory.type, "title": memory.title,
                "status": memory.status, "confidence": memory.confidence,
                "body": memory.body[:500], "updated": True,
            }, source=memory.source or "memory", correlation_id=memory.memory_id,
                sync_level=MEMORY_SYNC_LEVEL.get(memory.type))

    def delete_memory(self, memory_id: str) -> None:
        existing = self.get_memory(memory_id)
        self.db.execute("DELETE FROM memories WHERE memory_id=?", (memory_id,))
        if existing:
            self.emit("knowledge", {
                "memory_id": memory_id, "type": existing.type, "title": existing.title,
                "status": "deleted",
            }, source=existing.source or "memory", correlation_id=memory_id,
                sync_level=MEMORY_SYNC_LEVEL.get(existing.type))

    def resolve_memory_conflict(self, memory_id: str, *, keep: bool) -> None:
        target = self.get_memory(memory_id)
        if not target:
            raise NotFoundError(f"记忆不存在: {memory_id}")
        group = target.conflict_group
        if not group:
            self.update_memory(memory_id, status="active" if keep else "superseded")
            return
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT memory_id FROM memories WHERE conflict_group=?", (group,)).fetchall()
            for row in rows:
                mid = row["memory_id"]
                if mid == memory_id:
                    conn.execute("UPDATE memories SET status=?, updated_at=? WHERE memory_id=?",
                                 ("active" if keep else "superseded", now_iso(), mid))
                else:
                    conn.execute("UPDATE memories SET status=?, updated_at=? WHERE memory_id=?",
                                 ("superseded" if keep else "conflict", now_iso(), mid))

    def injectable_memories(self, project_id: str, *, scope: str = "") -> list[MemoryRecord]:
        memories = self.list_memories(project_id, status="active")
        out = []
        for m in memories:
            if not m.injectable:
                continue
            if m.expires_at and m.expires_at <= now_iso():
                continue
            if scope and m.scope and m.scope != scope:
                continue
            out.append(m)
        return out

    # ------------------------------------------------------------------
    # Conversations / decisions / cache
    # ------------------------------------------------------------------
    def upsert_conversation(self, record: ConversationRecord) -> None:
        self.db.execute(
            """
            INSERT INTO conversations(conversation_id, project_id, agent_id, session_id, source_path,
                title, cwd, started_at, ended_at, message_count, token_usage, summary, raw_path,
                imported_at, fingerprint)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(project_id, agent_id, session_id) DO UPDATE SET
                source_path=excluded.source_path, title=excluded.title, cwd=excluded.cwd,
                started_at=excluded.started_at, ended_at=excluded.ended_at,
                message_count=excluded.message_count, token_usage=excluded.token_usage,
                summary=excluded.summary, raw_path=excluded.raw_path,
                imported_at=excluded.imported_at, fingerprint=excluded.fingerprint
            """,
            (record.conversation_id, record.project_id, record.agent_id, record.session_id,
             record.source_path, record.title, record.cwd, record.started_at, record.ended_at,
             record.message_count, _dumps(record.token_usage), _dumps(record.summary),
             record.raw_path, record.imported_at, record.fingerprint),
        )
        self.emit("message", {
            "conversation_id": record.conversation_id, "agent_id": record.agent_id,
            "session_id": record.session_id, "title": record.title,
            "message_count": record.message_count, "token_usage": record.token_usage,
            "summary": _summary_digest(record.summary),
        }, source=record.agent_id, correlation_id=record.conversation_id, sync_level=1)

    def get_conversation(self, project_id: str, agent_id: str, session_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM conversations WHERE project_id=? AND agent_id=? AND session_id=?",
            (project_id, agent_id, session_id),
        )
        if row:
            row["token_usage"] = _j(row["token_usage"], {})
            row["summary"] = _j(row["summary"], {})
        return row

    def list_conversations(self, project_id: str, *, agent_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if agent_id:
            rows = self.db.query(
                "SELECT * FROM conversations WHERE project_id=? AND agent_id=? ORDER BY imported_at DESC LIMIT ?",
                (project_id, agent_id, limit),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM conversations WHERE project_id=? ORDER BY imported_at DESC LIMIT ?",
                (project_id, limit),
            )
        for r in rows:
            r["token_usage"] = _j(r["token_usage"], {})
            r["summary"] = _j(r["summary"], {})
        return rows

    def add_decision(self, decision: DecisionRecord) -> None:
        self.db.execute(
            "INSERT INTO decisions(decision_id, project_id, task_uid, agent_id, model, content, "
            "evidence, approved, approval_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (decision.decision_id, decision.project_id, decision.task_uid, decision.agent_id,
             decision.model, decision.content, _dumps(decision.evidence),
             1 if decision.approved else 0, decision.approval_id, decision.created_at),
        )
        self.emit("knowledge", {
            "decision_id": decision.decision_id, "content": decision.content[:500],
            "agent_id": decision.agent_id, "model": decision.model,
            "approved": decision.approved, "approval_id": decision.approval_id,
        }, source=decision.agent_id or "control-plane", correlation_id=decision.decision_id,
            sync_level=4)

    def list_decisions(self, project_id: str, *, limit: int = 200) -> list[DecisionRecord]:
        rows = self.db.query("SELECT * FROM decisions WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
                             (project_id, limit))
        return [
            DecisionRecord(
                decision_id=r["decision_id"], project_id=r["project_id"], content=r["content"],
                agent_id=r["agent_id"], model=r["model"], task_uid=r["task_uid"],
                evidence=_j(r["evidence"], []), approved=bool(r["approved"]),
                approval_id=r["approval_id"], created_at=r["created_at"],
            )
            for r in rows
        ]

    def cache_get(self, key: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM task_cache WHERE cache_key=?", (key,))
        if not row:
            return None
        if row["expires_at"] and row["expires_at"] <= now_iso():
            return None
        return _j(row["payload"], {})

    def cache_set(self, key: str, project_id: str, task_type: str, file_hash: str,
                  payload: dict[str, Any], expires_at: str = "") -> None:
        self.db.execute(
            "INSERT INTO task_cache(cache_key, project_id, task_type, file_hash, payload, created_at, expires_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload, "
            "created_at=excluded.created_at, expires_at=excluded.expires_at",
            (key, project_id, task_type, file_hash, _dumps(payload), now_iso(), expires_at),
        )

    def cache_clear(self, project_id: str) -> None:
        self.db.execute("DELETE FROM task_cache WHERE project_id=?", (project_id,))

    # ------------------------------------------------------------------
    # Context Bus
    # ------------------------------------------------------------------
    def append_bus_event(self, project_id: str, *, topic: str, kind: str, source: str,
                         payload: dict[str, Any], correlation_id: str = "",
                         sync_level: int = 0, tokens: int = 0) -> dict[str, Any]:
        with self.db.transaction() as conn:
            prev = conn.execute(
                "SELECT hash FROM bus_events WHERE project_id=? ORDER BY seq DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            prev_hash = prev["hash"] if prev else ""
            event_id = new_id("evt")
            ts = now_iso()
            base = {
                "event_id": event_id, "project_id": project_id, "topic": topic,
                "kind": kind, "source": source, "correlation_id": correlation_id,
                "sync_level": int(sync_level), "payload": payload, "tokens": int(tokens),
                "prev_hash": prev_hash, "created_at": ts,
            }
            digest = hash_obj(base)
            conn.execute(
                "INSERT INTO bus_events(event_id, project_id, topic, kind, source, correlation_id, "
                "sync_level, payload, tokens, prev_hash, hash, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, project_id, topic, kind, source, correlation_id, int(sync_level),
                 _dumps(payload), int(tokens), prev_hash, digest, ts),
            )
            row = conn.execute(
                "SELECT * FROM bus_events WHERE project_id=? ORDER BY seq DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            out = dict(row)
            out["payload"] = payload
            return out

    def list_bus_events(self, project_id: str, *, after_seq: int = 0, limit: int = 100,
                        topic: str = "", kind: str = "", correlation_id: str = "") -> list[dict[str, Any]]:
        clauses = ["project_id=?", "seq>?"]
        params: list[Any] = [project_id, int(after_seq)]
        if topic:
            clauses.append("topic=?")
            params.append(topic)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if correlation_id:
            clauses.append("correlation_id=?")
            params.append(correlation_id)
        rows = self.db.query(
            f"SELECT * FROM bus_events WHERE {' AND '.join(clauses)} ORDER BY seq ASC LIMIT ?",
            params + [int(limit)],
        )
        for row in rows:
            row["payload"] = _j(row["payload"], {})
        return rows

    def bus_stats(self, project_id: str) -> dict[str, Any]:
        totals = self.db.query_one(
            "SELECT COUNT(*) AS events, COALESCE(SUM(tokens),0) AS tokens, "
            "COALESCE(MAX(seq),0) AS last_seq FROM bus_events WHERE project_id=?",
            (project_id,),
        ) or {}
        by_kind = self.db.query(
            "SELECT kind, COUNT(*) AS events, COALESCE(SUM(tokens),0) AS tokens, MAX(seq) AS last_seq "
            "FROM bus_events WHERE project_id=? GROUP BY kind ORDER BY events DESC",
            (project_id,),
        )
        return {"totals": totals, "by_kind": by_kind}

    def verify_bus_chain(self, project_id: str) -> tuple[bool, list[str]]:
        rows = self.db.query(
            "SELECT * FROM bus_events WHERE project_id=? ORDER BY seq ASC", (project_id,)
        )
        prev = ""
        errors: list[str] = []
        for row in rows:
            base = {
                "event_id": row["event_id"], "project_id": row["project_id"],
                "topic": row["topic"], "kind": row["kind"], "source": row["source"],
                "correlation_id": row["correlation_id"], "sync_level": int(row["sync_level"]),
                "payload": _j(row["payload"], {}), "tokens": int(row["tokens"]),
                "prev_hash": prev, "created_at": row["created_at"],
            }
            expected = hash_obj(base)
            if row["prev_hash"] != prev:
                errors.append(f"seq {row['seq']}: prev_hash 不匹配")
            if row["hash"] != expected:
                errors.append(f"seq {row['seq']}: hash 不匹配")
            prev = row["hash"]
        return (not errors, errors)

    def create_bus_subscription(self, project_id: str, subscriber: str, *,
                                topics: list[str], kinds: list[str],
                                cursor_seq: int = 0) -> dict[str, Any]:
        existing = self.db.query_one(
            "SELECT * FROM bus_subscriptions WHERE project_id=? AND subscriber=? AND active=1 "
            "ORDER BY created_at DESC LIMIT 1",
            (project_id, subscriber),
        )
        if existing:
            self.db.execute(
                "UPDATE bus_subscriptions SET topics=?, kinds=?, updated_at=? WHERE subscription_id=?",
                (_dumps(topics), _dumps(kinds), now_iso(), existing["subscription_id"]),
            )
            row = self.db.query_one(
                "SELECT * FROM bus_subscriptions WHERE subscription_id=?", (existing["subscription_id"],)
            )
        else:
            sub_id = new_id("sub")
            self.db.execute(
                "INSERT INTO bus_subscriptions(subscription_id, project_id, subscriber, topics, kinds, "
                "cursor_seq, active, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (sub_id, project_id, subscriber, _dumps(topics), _dumps(kinds),
                 int(cursor_seq), 1, now_iso(), now_iso()),
            )
            row = self.db.query_one("SELECT * FROM bus_subscriptions WHERE subscription_id=?", (sub_id,))
        out = dict(row) if row else {}
        if out:
            out["topics"] = _j(out.get("topics"), [])
            out["kinds"] = _j(out.get("kinds"), [])
        return out

    def get_bus_subscription(self, subscription_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM bus_subscriptions WHERE subscription_id=?", (subscription_id,))
        if not row:
            return None
        row["topics"] = _j(row["topics"], [])
        row["kinds"] = _j(row["kinds"], [])
        return row

    def list_bus_subscriptions(self, project_id: str, *, active_only: bool = True) -> list[dict[str, Any]]:
        where = "project_id=?" + (" AND active=1" if active_only else "")
        rows = self.db.query(
            f"SELECT * FROM bus_subscriptions WHERE {where} ORDER BY created_at DESC",
            (project_id,),
        )
        for row in rows:
            row["topics"] = _j(row["topics"], [])
            row["kinds"] = _j(row["kinds"], [])
        return rows

    def update_bus_cursor(self, subscription_id: str, cursor_seq: int) -> None:
        self.db.execute(
            "UPDATE bus_subscriptions SET cursor_seq=?, updated_at=? WHERE subscription_id=?",
            (int(cursor_seq), now_iso(), subscription_id),
        )

    def deactivate_bus_subscription(self, subscription_id: str) -> None:
        self.db.execute(
            "UPDATE bus_subscriptions SET active=0, updated_at=? WHERE subscription_id=?",
            (now_iso(), subscription_id),
        )

    # ------------------------------------------------------------------
    # Capability Router
    # ------------------------------------------------------------------
    def record_routing(self, *, project_id: str, decision_id: str, agent_id: str,
                       score: float, breakdown: dict[str, Any], candidates: list[dict[str, Any]],
                       reason: str, run_id: str = "", task_uid: str = "",
                       task_id: str = "") -> None:
        self.db.execute(
            "INSERT INTO routing_decisions(decision_id, project_id, run_id, task_uid, task_id, "
            "agent_id, score, breakdown, candidates, reason, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, project_id, run_id, task_uid, task_id, agent_id, float(score),
             _dumps(breakdown), _dumps(candidates), reason, now_iso()),
        )
        self.emit("routing", {
            "decision_id": decision_id, "agent_id": agent_id, "score": round(float(score), 4),
            "reason": reason, "required_capabilities": (breakdown or {}).get("required", []),
        }, correlation_id=task_uid or run_id)

    def latest_routing(self, task_uid: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM routing_decisions WHERE task_uid=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (task_uid,),
        )
        if not row:
            return None
        row["breakdown"] = _j(row["breakdown"], {})
        row["candidates"] = _j(row["candidates"], [])
        return row

    def list_routing(self, project_id: str, *, run_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if run_id:
            rows = self.db.query(
                "SELECT * FROM routing_decisions WHERE project_id=? AND run_id=? "
                "ORDER BY created_at DESC LIMIT ?", (project_id, run_id, limit),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM routing_decisions WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            )
        for row in rows:
            row["breakdown"] = _j(row["breakdown"], {})
            row["candidates"] = _j(row["candidates"], [])
        return rows

    def agent_usage_stats(self, project_id: str) -> dict[str, dict[str, Any]]:
        rows = self.db.query(
            "SELECT agent_id, COUNT(*) AS records, COALESCE(SUM(total_tokens),0) AS total_tokens, "
            "COALESCE(SUM(cost),0) AS total_cost, COALESCE(AVG(duration_ms),0) AS avg_duration_ms, "
            "COALESCE(SUM(tool_calls),0) AS tool_calls, COALESCE(SUM(retries),0) AS retries "
            "FROM usage_records WHERE project_id=? GROUP BY agent_id",
            (project_id,),
        )
        return {row["agent_id"]: row for row in rows}

    # ------------------------------------------------------------------
    # Semantic cache
    # ------------------------------------------------------------------
    def semantic_candidates(self, project_id: str, task_type: str, file_hash: str,
                            limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM semantic_cache WHERE project_id=? AND task_type=? AND file_hash=? "
            "ORDER BY created_at DESC LIMIT ?",
            (project_id, task_type, file_hash, int(limit)),
        )
        out = []
        now = now_iso()
        for row in rows:
            if row["expires_at"] and row["expires_at"] <= now:
                continue
            row["payload"] = _j(row["payload"], {})
            out.append(row)
        return out

    def semantic_set(self, cache_key: str, project_id: str, task_type: str, signature: str,
                     objective: str, file_hash: str, payload: dict[str, Any],
                     expires_at: str = "") -> None:
        self.db.execute(
            "INSERT INTO semantic_cache(cache_key, project_id, task_type, signature, objective, "
            "file_hash, payload, hits, created_at, last_hit_at, expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
            "signature=excluded.signature, objective=excluded.objective, payload=excluded.payload, "
            "expires_at=excluded.expires_at",
            (cache_key, project_id, task_type, signature, objective, file_hash, _dumps(payload),
             0, now_iso(), "", expires_at),
        )

    def semantic_hit(self, cache_key: str) -> None:
        self.db.execute(
            "UPDATE semantic_cache SET hits=hits+1, last_hit_at=? WHERE cache_key=?",
            (now_iso(), cache_key),
        )

    def semantic_clear(self, project_id: str) -> None:
        self.db.execute("DELETE FROM semantic_cache WHERE project_id=?", (project_id,))

    def semantic_stats(self, project_id: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT COUNT(*) AS entries, COALESCE(SUM(hits),0) AS hits FROM semantic_cache WHERE project_id=?",
            (project_id,),
        ) or {}
        return {"entries": int(row.get("entries", 0)), "hits": int(row.get("hits", 0))}

    # ------------------------------------------------------------------
    # Savings ledger / Agent benchmark
    # ------------------------------------------------------------------
    def add_saving(self, *, saving_id: str, project_id: str, kind: str, tokens_saved: int,
                   cost_saved: float = 0.0, task_uid: str = "", run_id: str = "",
                   detail: dict[str, Any] | None = None) -> None:
        self.db.execute(
            "INSERT INTO savings_ledger(saving_id, project_id, kind, task_uid, run_id, "
            "tokens_saved, cost_saved, detail, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (saving_id, project_id, kind, task_uid, run_id, int(tokens_saved),
             float(cost_saved), _dumps(detail or {}), now_iso()),
        )
        self.emit("cost", {
            "saving_id": saving_id, "kind": kind, "tokens_saved": int(tokens_saved),
            "cost_saved": round(float(cost_saved), 6), "detail": detail or {},
        }, correlation_id=task_uid or run_id, sync_level=0)

    def savings_totals(self, project_id: str, *, run_id: str = "") -> dict[str, Any]:
        where = "project_id=?" + (" AND run_id=?" if run_id else "")
        params: list[Any] = [project_id] + ([run_id] if run_id else [])
        row = self.db.query_one(
            "SELECT COUNT(*) AS records, COALESCE(SUM(tokens_saved),0) AS tokens_saved, "
            f"COALESCE(SUM(cost_saved),0) AS cost_saved FROM savings_ledger WHERE {where}",
            params,
        )
        return row or {}

    def savings_by_kind(self, project_id: str, *, run_id: str = "") -> list[dict[str, Any]]:
        where = "project_id=?" + (" AND run_id=?" if run_id else "")
        params: list[Any] = [project_id] + ([run_id] if run_id else [])
        return self.db.query(
            "SELECT kind, COUNT(*) AS records, COALESCE(SUM(tokens_saved),0) AS tokens_saved, "
            f"COALESCE(SUM(cost_saved),0) AS cost_saved FROM savings_ledger WHERE {where} "
            "GROUP BY kind ORDER BY tokens_saved DESC",
            params,
        )

    def list_savings(self, project_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM savings_ledger WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
        )
        for row in rows:
            row["detail"] = _j(row["detail"], {})
        return rows

    def save_benchmark(self, *, benchmark_id: str, project_id: str, agent_id: str,
                       snapshot: dict[str, Any], score: float) -> None:
        self.db.execute(
            "INSERT INTO agent_benchmarks(benchmark_id, project_id, agent_id, snapshot, score, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (benchmark_id, project_id, agent_id, _dumps(snapshot), float(score), now_iso()),
        )

    def latest_benchmarks(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM agent_benchmarks WHERE project_id=? ORDER BY created_at DESC LIMIT 500",
            (project_id,),
        )
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for row in rows:
            if row["agent_id"] in seen:
                continue
            seen.add(row["agent_id"])
            row["snapshot"] = _j(row["snapshot"], {})
            out.append(row)
        return out

    def benchmark_history(self, project_id: str, *, agent_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if agent_id:
            rows = self.db.query(
                "SELECT * FROM agent_benchmarks WHERE project_id=? AND agent_id=? "
                "ORDER BY created_at DESC LIMIT ?", (project_id, agent_id, limit),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM agent_benchmarks WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            )
        for row in rows:
            row["snapshot"] = _j(row["snapshot"], {})
        return rows
