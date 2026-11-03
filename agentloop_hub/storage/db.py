"""SQLite persistence with thread-local connections and an append-only audit log."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from ..errors import HubError
from ..util import ensure_dir


SCHEMA_VERSION = 3

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    worktree_id TEXT NOT NULL,
    git_remote TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    commit_hash TEXT NOT NULL DEFAULT '',
    tech_stack TEXT NOT NULL DEFAULT '[]',
    build_command TEXT NOT NULL DEFAULT '',
    test_command TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    extra TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    product TEXT NOT NULL,
    edition TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT 'unknown',
    adapter_type TEXT NOT NULL,
    adapter_config TEXT NOT NULL DEFAULT '{}',
    access_level TEXT NOT NULL DEFAULT 'L1',
    capabilities TEXT NOT NULL DEFAULT '{}',
    models TEXT NOT NULL DEFAULT '[]',
    permissions TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'unknown',
    last_seen TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    discovered INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    extra TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_agents_project ON agents(project_id);

CREATE TABLE IF NOT EXISTS main_nodes (
    revision INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'project',
    budget_tokens INTEGER NOT NULL DEFAULT 0,
    permissions TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1,
    set_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_main_project ON main_nodes(project_id, active);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    goal TEXT NOT NULL,
    constraints TEXT NOT NULL DEFAULT '[]',
    tasks TEXT NOT NULL DEFAULT '[]',
    main_agent_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    raw TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    approved_at TEXT NOT NULL DEFAULT '',
    approval_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_plans_project ON plans(project_id, created_at);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    status TEXT NOT NULL,
    main_agent_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    base_commit TEXT NOT NULL DEFAULT '',
    base_branch TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    acceptance_status TEXT NOT NULL DEFAULT 'pending',
    release_status TEXT NOT NULL DEFAULT 'not_requested',
    created_at TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    acceptance_at TEXT NOT NULL DEFAULT '',
    release_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id, created_at);

CREATE TABLE IF NOT EXISTS tasks (
    uid TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    parent_task_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    type TEXT NOT NULL,
    suggested_agent TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '[]',
    out_of_scope TEXT NOT NULL DEFAULT '[]',
    dependencies TEXT NOT NULL DEFAULT '[]',
    required_capabilities TEXT NOT NULL DEFAULT '[]',
    input_artifacts TEXT NOT NULL DEFAULT '[]',
    expected_outputs TEXT NOT NULL DEFAULT '[]',
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    verification_commands TEXT NOT NULL DEFAULT '[]',
    token_budget INTEGER NOT NULL DEFAULT 0,
    time_limit_minutes INTEGER NOT NULL DEFAULT 0,
    retry_limit INTEGER NOT NULL DEFAULT 0,
    risk_level TEXT NOT NULL DEFAULT 'low',
    approval_policy TEXT NOT NULL DEFAULT 'auto',
    priority INTEGER NOT NULL DEFAULT 5,
    assigned_agent TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    workdir TEXT NOT NULL DEFAULT '',
    task_dir TEXT NOT NULL DEFAULT '',
    base_commit TEXT NOT NULL DEFAULT '',
    patch_path TEXT NOT NULL DEFAULT '',
    result_path TEXT NOT NULL DEFAULT '',
    verification_status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    pid INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_plan_task ON tasks(plan_id, task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks(run_id, status);

CREATE TABLE IF NOT EXISTS results (
    task_uid TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    valid INTEGER NOT NULL DEFAULT 0,
    validation_error TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verifications (
    verification_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_uid TEXT NOT NULL,
    task_id TEXT NOT NULL,
    verifier_agent TEXT NOT NULL,
    status TEXT NOT NULL,
    checks TEXT NOT NULL DEFAULT '{}',
    warnings TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    report_path TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'control_plane',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verifications_task ON verifications(task_uid, created_at);

CREATE TABLE IF NOT EXISTS changes (
    change_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_uid TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    base_commit TEXT NOT NULL DEFAULT '',
    base_hash TEXT NOT NULL DEFAULT '',
    before_hash TEXT NOT NULL DEFAULT '',
    after_hash TEXT NOT NULL DEFAULT '',
    files TEXT NOT NULL DEFAULT '[]',
    patch_path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'proposed',
    created_at TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_changes_task ON changes(task_uid);

CREATE TABLE IF NOT EXISTS file_leases (
    lease_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    path_pattern TEXT NOT NULL,
    task_uid TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'write',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    released_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_leases_project ON file_leases(project_id, active);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    plan_id TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    task_uid TEXT NOT NULL DEFAULT '',
    requested_at TEXT NOT NULL,
    decided_at TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_approvals_project ON approvals(project_id, status, requested_at);

CREATE TABLE IF NOT EXISTS usage_records (
    record_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_uid TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    agent_id TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    estimated INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_project ON usage_records(project_id, created_at);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    task_uid TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_project ON checkpoints(project_id, created_at);

CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    prev_hash TEXT NOT NULL DEFAULT '',
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_project ON audit(project_id, seq);

CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN
    SELECT RAISE(ABORT, 'audit log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN
    SELECT RAISE(ABORT, 'audit log is append-only');
END;

CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.8,
    scope TEXT NOT NULL DEFAULT '',
    task_uid TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL DEFAULT '',
    invalid_conditions TEXT NOT NULL DEFAULT '[]',
    last_verified_at TEXT NOT NULL DEFAULT '',
    injectable INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    conflict_group TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project_id, status);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    ended_at TEXT NOT NULL DEFAULT '',
    message_count INTEGER NOT NULL DEFAULT 0,
    token_usage TEXT NOT NULL DEFAULT '{}',
    summary TEXT NOT NULL DEFAULT '{}',
    raw_path TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL,
    fingerprint TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_source ON conversations(project_id, agent_id, session_id);
CREATE INDEX IF NOT EXISTS idx_conversations_project ON conversations(project_id, imported_at);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_uid TEXT NOT NULL DEFAULT '',
    agent_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]',
    approved INTEGER NOT NULL DEFAULT 0,
    approval_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_cache (
    cache_key TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- AgentMesh OS v2: Context Bus, Capability Router, Cost Optimizer, Governance
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bus_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'control-plane',
    correlation_id TEXT NOT NULL DEFAULT '',
    sync_level INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL DEFAULT '{}',
    tokens INTEGER NOT NULL DEFAULT 0,
    prev_hash TEXT NOT NULL DEFAULT '',
    hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bus_project_seq ON bus_events(project_id, seq);
CREATE INDEX IF NOT EXISTS idx_bus_kind ON bus_events(project_id, kind, seq);
CREATE INDEX IF NOT EXISTS idx_bus_correlation ON bus_events(project_id, correlation_id, seq);

CREATE TRIGGER IF NOT EXISTS bus_no_update BEFORE UPDATE ON bus_events
BEGIN
    SELECT RAISE(ABORT, 'context bus is append-only');
END;

CREATE TRIGGER IF NOT EXISTS bus_no_delete BEFORE DELETE ON bus_events
BEGIN
    SELECT RAISE(ABORT, 'context bus is append-only');
END;

CREATE TABLE IF NOT EXISTS bus_subscriptions (
    subscription_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    subscriber TEXT NOT NULL,
    topics TEXT NOT NULL DEFAULT '[]',
    kinds TEXT NOT NULL DEFAULT '[]',
    cursor_seq INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bus_sub_project ON bus_subscriptions(project_id, active);

CREATE TABLE IF NOT EXISTS routing_decisions (
    decision_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    task_uid TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    agent_id TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    breakdown TEXT NOT NULL DEFAULT '{}',
    candidates TEXT NOT NULL DEFAULT '[]',
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_routing_task ON routing_decisions(task_uid, created_at);

CREATE TABLE IF NOT EXISTS semantic_cache (
    cache_key TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    signature TEXT NOT NULL,
    objective TEXT NOT NULL DEFAULT '',
    file_hash TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    hits INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_hit_at TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_semantic_project ON semantic_cache(project_id, task_type, file_hash);

CREATE TABLE IF NOT EXISTS savings_ledger (
    saving_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    task_uid TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    tokens_saved INTEGER NOT NULL DEFAULT 0,
    cost_saved REAL NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_savings_project ON savings_ledger(project_id, created_at);

CREATE TABLE IF NOT EXISTS agent_benchmarks (
    benchmark_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    snapshot TEXT NOT NULL DEFAULT '{}',
    score REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_benchmarks_project ON agent_benchmarks(project_id, created_at);

-- ---------------------------------------------------------------------------
-- AgentMesh OS v3: Agent software version ledger + model catalog
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agent_versions (
    revision INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT 'unknown',
    adapter_type TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    capabilities TEXT NOT NULL DEFAULT '{}',
    models TEXT NOT NULL DEFAULT '[]',
    changes TEXT NOT NULL DEFAULT '{}',
    compat_status TEXT NOT NULL DEFAULT 'ok',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_versions ON agent_versions(project_id, agent_id, revision);

CREATE TABLE IF NOT EXISTS agent_models (
    project_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    model TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'discovered',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id, agent_id, model)
);
CREATE INDEX IF NOT EXISTS idx_agent_models ON agent_models(project_id, agent_id);
"""


class Database:
    """Thread-safe SQLite wrapper.

    Each thread gets its own connection; WAL keeps readers from blocking the
    orchestration threads. No global mutable cursor is ever shared.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        ensure_dir(Path(self.path).parent)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self.initialize()

    # -- connection -------------------------------------------------------
    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close_thread(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def initialize(self) -> None:
        with self._init_lock:
            conn = self.connection()
            conn.executescript(SCHEMA)
            self._ensure_columns(conn)
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
            else:
                version = int(row["value"])
                if version > SCHEMA_VERSION:
                    raise HubError(f"数据库版本 {version} 高于当前程序支持的 {SCHEMA_VERSION}")
                if version < SCHEMA_VERSION:
                    # v1 -> v3 is additive: new tables are created by SCHEMA and
                    # _ensure_columns adds the new columns in place. Existing
                    # workspaces are upgraded without touching their data.
                    conn.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                                 (str(SCHEMA_VERSION),))

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection) -> None:
        """Additive compatibility for workspaces created during development."""
        def columns(table: str) -> set[str]:
            return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}

        task_columns = columns("tasks")
        if "suggested_agent" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN suggested_agent TEXT NOT NULL DEFAULT ''")
        if "priority" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 5")
        agent_columns = columns("agents")
        if "extra" not in agent_columns:
            conn.execute("ALTER TABLE agents ADD COLUMN extra TEXT NOT NULL DEFAULT '{}'")
        run_columns = columns("runs")
        if "acceptance_status" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN acceptance_status TEXT NOT NULL DEFAULT 'pending'")
        if "release_status" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN release_status TEXT NOT NULL DEFAULT 'not_requested'")
        if "acceptance_at" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN acceptance_at TEXT NOT NULL DEFAULT ''")
        if "release_at" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN release_at TEXT NOT NULL DEFAULT ''")

    # -- primitives -------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Cursor:
        return self.connection().execute(sql, params or ())

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        return self.connection().executemany(sql, seq)

    def query(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        rows = self.connection().execute(sql, params or ()).fetchall()
        return [dict(r) for r in rows]

    def query_one(self, sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
        row = self.connection().execute(sql, params or ()).fetchone()
        return dict(row) if row else None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.query_one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
