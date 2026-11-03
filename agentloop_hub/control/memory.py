"""Unified Memory: the project brain every Agent reads at startup.

需求整理-v1 模块 2 要求统一存储聊天记录、任务状态、项目索引、知识库、
代码结构与决策过程。本服务提供：

* 八类规范记忆（message/task_state/project_index/code_structure/knowledge/
  decision/workflow/user_preference）；
* 冲突检测与人工裁决；
* 确定性同步：会话导入、任务状态变化、工作流阶段、项目/代码索引；
* Agent 启动引导包（bootstrap）：任何 Agent 进入项目先读同一份状态。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from ..config import HubConfig
from ..errors import NotFoundError, ValidationError
from ..models import (
    MEMORY_SYNC_LEVEL, ConversationRecord, MemoryRecord, MemoryStatus, MemoryType,
    Project, normalize_memory_type,
)
from ..storage.repo import Store
from ..util import estimate_tokens, new_id, now_iso, truncate


SOURCE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".cs", ".cpp",
    ".c", ".h", ".hpp", ".rb", ".php", ".dart", ".kt", ".swift", ".scala", ".sh",
    ".ps1", ".sql", ".vue", ".svelte",
}

KEY_FILE_PATTERNS = (
    "README.md", "README*", "pyproject.toml", "package.json", "requirements.txt",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "CMakeLists.txt", "Dockerfile",
    "docker-compose.yml", "Makefile", ".env.example",
)


class MemoryService:
    def __init__(self, store: Store, project: Project, config: HubConfig):
        self.store = store
        self.project = project
        self.config = config
        self._sync_lock = threading.Lock()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def add(self, *, type: str, title: str, body: str, source: str = "user",
            confidence: float = 0.8, scope: str = "", task_uid: str = "",
            expires_at: str = "", invalid_conditions: list[str] | None = None,
            injectable: bool = True) -> MemoryRecord:
        memory = MemoryRecord(
            memory_id=new_id("mem"), project_id=self.project.project_id,
            type=normalize_memory_type(type), title=title.strip(), body=body.strip(),
            source=source, confidence=max(0.0, min(1.0, float(confidence))), scope=scope,
            task_uid=task_uid, expires_at=expires_at,
            invalid_conditions=invalid_conditions or [], last_verified_at=now_iso(),
            injectable=injectable, created_at=now_iso(), updated_at=now_iso(),
        )
        memory = self.store.add_memory(memory)
        self.store.append_audit(self.project.project_id, "memory", "memory.added",
                                subject=memory.memory_id,
                                payload={"type": memory.type, "title": title,
                                         "status": memory.status,
                                         "sync_level": MEMORY_SYNC_LEVEL.get(memory.type, 0)})
        return memory

    def upsert_active(self, *, type: str, title: str, body: str, source: str,
                      confidence: float = 0.9, scope: str = "") -> MemoryRecord:
        """Create or update one deterministic, auto-synced memory record.

        A process-local lock prevents concurrent task completions from creating
        duplicate run-state records for the same title.
        """
        normalized = normalize_memory_type(type)
        with self._sync_lock:
            existing = next(
                (m for m in self.store.list_memories(self.project.project_id, limit=10000)
                 if m.type == normalized and m.title == title),
                None,
            )
            if existing:
                self.store.update_memory(
                    existing.memory_id, body=body, source=source,
                    confidence=float(confidence), scope=scope, last_verified_at=now_iso(),
                )
                self.store.append_audit(self.project.project_id, "memory", "memory.synced",
                                        subject=existing.memory_id,
                                        payload={"type": normalized, "title": title})
                return self.get(existing.memory_id)
            return self.add(type=normalized, title=title, body=body, source=source,
                            confidence=confidence, scope=scope)

    def get(self, memory_id: str) -> MemoryRecord:
        memory = self.store.get_memory(memory_id)
        if not memory:
            raise NotFoundError(f"记忆不存在: {memory_id}")
        return memory

    def list(self, *, type: str = "", status: str = "", limit: int = 500) -> list[MemoryRecord]:
        return self.store.list_memories(
            self.project.project_id, type=normalize_memory_type(type) if type else "",
            status=status, limit=limit,
        )

    def search(self, keyword: str, *, limit: int = 100) -> list[MemoryRecord]:
        needle = keyword.strip().lower()
        if not needle:
            return self.list(limit=limit)
        rows = self.list(limit=max(limit * 4, 500))
        return [m for m in rows if needle in m.title.lower() or needle in m.body.lower()][:limit]

    def update(self, memory_id: str, **fields: Any) -> MemoryRecord:
        allowed = {k: v for k, v in fields.items() if v is not None}
        if not allowed:
            return self.get(memory_id)
        if "status" in allowed and allowed["status"] not in {s.value for s in MemoryStatus}:
            raise ValidationError(f"未知记忆状态: {allowed['status']}")
        if "type" in allowed:
            allowed["type"] = normalize_memory_type(allowed["type"])
        self.store.update_memory(memory_id, **allowed)
        self.store.append_audit(self.project.project_id, "memory", "memory.updated",
                                subject=memory_id, payload={"fields": list(allowed)})
        return self.get(memory_id)

    def delete(self, memory_id: str) -> None:
        self.get(memory_id)
        self.store.delete_memory(memory_id)
        self.store.append_audit(self.project.project_id, "memory", "memory.deleted",
                                subject=memory_id)

    def resolve_conflict(self, memory_id: str, *, keep: bool) -> None:
        self.store.resolve_memory_conflict(memory_id, keep=keep)
        self.store.append_audit(self.project.project_id, "memory", "memory.conflict_resolved",
                                subject=memory_id, payload={"keep": keep})

    def injectable(self, *, scope: str = "") -> list[dict[str, Any]]:
        memories = self.store.injectable_memories(self.project.project_id, scope=scope)
        return [
            {
                "memory_id": m.memory_id, "type": m.type, "title": m.title,
                "body": truncate(m.body, 1200), "confidence": m.confidence,
                "source": m.source,
            }
            for m in memories[:50]
        ]

    def export_all(self) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self.list(limit=10000)]

    # ------------------------------------------------------------------
    # Deterministic synchronization
    # ------------------------------------------------------------------
    def seed_project_facts(self) -> list[MemoryRecord]:
        """Record deterministic project facts (project_index category)."""
        facts = [
            ("技术栈", ", ".join(self.project.tech_stack) or "未识别"),
            ("项目根目录", self.project.root),
            ("构建命令", self.project.build_command or "未配置"),
            ("测试命令", self.project.test_command or "未配置"),
            ("Git 分支", self.project.branch or "非 Git 项目"),
        ]
        created: list[MemoryRecord] = []
        for title, body in facts:
            record = self.upsert_active(
                type=MemoryType.PROJECT_INDEX.value, title=title, body=body,
                source="workspace-scan", confidence=1.0,
            )
            created.append(record)
        return created

    def seed_project_index(self) -> MemoryRecord:
        inventory = self._scan_workspace()
        body = json.dumps({
            "root": self.project.root,
            "branch": self.project.branch,
            "commit": self.project.commit,
            "tech_stack": self.project.tech_stack,
            "build_command": self.project.build_command,
            "test_command": self.project.test_command,
            "top_level_dirs": inventory["top_dirs"],
            "top_level_files": inventory["top_files"],
            "file_count_scanned": inventory["file_count"],
            "by_extension": inventory["by_extension"],
            "key_files": inventory["key_files"],
            "scan_truncated": inventory["truncated"],
        }, ensure_ascii=False, indent=2)
        return self.upsert_active(
            type=MemoryType.PROJECT_INDEX.value, title="项目索引",
            body=body, source="workspace-scan", confidence=1.0,
        )

    def seed_code_structure(self) -> MemoryRecord:
        inventory = self._scan_workspace()
        body = json.dumps({
            "source_dirs": inventory["source_dirs"],
            "test_dirs": inventory["test_dirs"],
            "entrypoints": inventory["entrypoints"],
            "source_file_count": inventory["source_files"],
            "top_level_dirs": inventory["top_dirs"],
            "scan_truncated": inventory["truncated"],
        }, ensure_ascii=False, indent=2)
        return self.upsert_active(
            type=MemoryType.CODE_STRUCTURE.value, title="代码结构",
            body=body, source="workspace-scan", confidence=1.0,
        )

    def sync_task_state(self, run_id: str) -> MemoryRecord | None:
        run = self.store.get_run(run_id)
        if not run:
            return None
        tasks = self.store.list_tasks(run_id)
        plan = self.store.get_plan(run["plan_id"])
        total = len(tasks) or 1
        completed = sum(1 for t in tasks if t.status == "completed")
        body = {
            "goal": (plan.goal if plan else ""),
            "run_id": run_id,
            "run_status": run["status"],
            "progress": round(completed / total, 4),
            "counts": _count_statuses(tasks),
            "tasks": [
                {
                    "task_id": t.task_id, "status": t.status,
                    "agent": t.assigned_agent, "attempts": t.attempts,
                    "verification": t.verification_status,
                }
                for t in tasks
            ],
            "updated_at": now_iso(),
        }
        return self.upsert_active(
            type=MemoryType.TASK_STATE.value, title=f"任务状态 {run_id}",
            body=json.dumps(body, ensure_ascii=False), source="control-plane",
            confidence=1.0,
        )

    def sync_workflow(self, run_id: str) -> MemoryRecord | None:
        run = self.store.get_run(run_id)
        if not run:
            return None
        tasks = self.store.list_tasks(run_id)
        plan = self.store.get_plan(run["plan_id"])
        total = len(tasks) or 1
        completed = sum(1 for t in tasks if t.status == "completed")
        pending = [a.kind for a in self.store.list_approvals(
            self.project.project_id, status="pending", limit=100)
            if a.run_id == run_id]
        body = {
            "goal": plan.goal if plan else "",
            "phase": run["status"],
            "progress": round(completed / total, 4),
            "acceptance_status": run.get("acceptance_status", "pending"),
            "release_status": run.get("release_status", "not_requested"),
            "pending_approvals": pending,
            "updated_at": now_iso(),
        }
        return self.upsert_active(
            type=MemoryType.WORKFLOW.value, title=f"工作流状态 {run_id}",
            body=json.dumps(body, ensure_ascii=False), source="control-plane",
            confidence=1.0,
        )

    def sync_conversation(self, record: ConversationRecord) -> MemoryRecord:
        summary = record.summary if isinstance(record.summary, dict) else {}
        body = {
            "agent_id": record.agent_id,
            "session_id": record.session_id,
            "title": record.title,
            "message_count": record.message_count,
            "cwd": record.cwd,
            "token_usage": record.token_usage,
            "user_requests": list(summary.get("user_requests", []))[:10],
            "last_assistant_reply": str(summary.get("last_assistant_reply", ""))[:800],
            "tool_names": list(summary.get("tool_names", []))[:50],
        }
        return self.upsert_active(
            type=MemoryType.MESSAGE.value,
            title=f"会话 {record.agent_id}/{record.session_id}",
            body=json.dumps(body, ensure_ascii=False), source=record.agent_id,
            confidence=0.8,
        )

    def record_decision(self, content: str, *, agent_id: str = "", model: str = "",
                        task_uid: str = "", evidence: list[Any] | None = None,
                        approved: bool = False) -> MemoryRecord:
        return self.add(
            type=MemoryType.DECISION.value, title=content[:80], body=content,
            source=f"agent:{agent_id}" if agent_id else "control-plane",
            confidence=1.0 if approved else 0.7, task_uid=task_uid,
            invalid_conditions=["任务目标变更", "用户否决"],
        )

    def record_problem(self, *, symptom: str, cause: str, fix: str,
                       verification: str, task_uid: str = "") -> MemoryRecord:
        body = f"症状：{symptom}\n根因：{cause}\n修复：{fix}\n验证：{verification}"
        return self.add(
            type=MemoryType.KNOWLEDGE.value, title=symptom[:80], body=body,
            source="verification", confidence=0.8, task_uid=task_uid,
            invalid_conditions=["相关文件基线变化"],
        )

    def record_knowledge(self, title: str, body: str, *, source: str = "user",
                         confidence: float = 0.8) -> MemoryRecord:
        return self.add(type=MemoryType.KNOWLEDGE.value, title=title, body=body,
                        source=source, confidence=confidence)

    # ------------------------------------------------------------------
    # Agent startup bootstrap
    # ------------------------------------------------------------------
    def bootstrap(self, *, scope: str = "", agent_id: str = "") -> dict[str, Any]:
        limit = max(1, int(self.config.memory_bootstrap_limit))
        groups: dict[str, list[dict[str, Any]]] = {}
        for memory_type in (
            MemoryType.PROJECT_INDEX, MemoryType.CODE_STRUCTURE,
            MemoryType.TASK_STATE, MemoryType.WORKFLOW, MemoryType.KNOWLEDGE,
            MemoryType.DECISION, MemoryType.MESSAGE, MemoryType.USER_PREFERENCE,
        ):
            records = self.store.injectable_memories(
                self.project.project_id, scope=scope
            )
            selected = [m for m in records if m.type == memory_type.value][:limit]
            groups[memory_type.value] = [
                {
                    "memory_id": m.memory_id, "title": m.title,
                    "body": truncate(m.body, 900), "confidence": m.confidence,
                    "source": m.source, "updated_at": m.updated_at,
                    "sync_level": MEMORY_SYNC_LEVEL.get(m.type, 0),
                }
                for m in selected
            ]
        bus_events = [
            {
                "seq": e["seq"], "kind": e["kind"], "topic": e["topic"],
                "source": e["source"], "created_at": e["created_at"],
                "payload": e["payload"],
            }
            for e in self.store.list_bus_events(self.project.project_id, after_seq=0, limit=limit)
        ]
        counts = {
            level: sum(
                len(groups.get(mtype, [])) for mtype, lvl in MEMORY_SYNC_LEVEL.items() if lvl == level
            )
            for level in (1, 2, 3, 4, 5)
        }
        packet = {
            "generated_at": now_iso(),
            "agent_id": agent_id,
            "project": {
                "project_id": self.project.project_id, "name": self.project.name,
                "root": self.project.root, "branch": self.project.branch,
                "commit": self.project.commit, "tech_stack": self.project.tech_stack,
                "build_command": self.project.build_command,
                "test_command": self.project.test_command,
            },
            "groups": groups,
            "recent_bus": bus_events,
            "sync_levels": {
                "message": counts.get(1, 0), "task_state": counts.get(2, 0),
                "project_context": counts.get(3, 0), "knowledge": counts.get(4, 0),
                "workflow": counts.get(5, 0),
            },
        }
        # Keep the bootstrap packet bounded so it can be injected into every agent.
        text = json.dumps(packet, ensure_ascii=False, default=str)
        if estimate_tokens(text) > self.config.context_summary_tokens:
            packet["_truncated"] = True
            for key in ("recent_bus", "groups"):
                if estimate_tokens(json.dumps(packet, ensure_ascii=False, default=str)) <= \
                        self.config.context_summary_tokens:
                    break
                if key == "recent_bus":
                    packet[key] = packet[key][:3]
                else:
                    for group in packet[key].values():
                        while group and estimate_tokens(
                            json.dumps(packet, ensure_ascii=False, default=str)
                        ) > self.config.context_summary_tokens:
                            group.pop()
        return packet

    # ------------------------------------------------------------------
    # Workspace inventory (bounded, deterministic)
    # ------------------------------------------------------------------
    def _scan_workspace(self, *, max_files: int = 2500) -> dict[str, Any]:
        base = Path(self.project.root)
        excluded_dirs = {
            ".git", ".agentloop", ".agentloop-task", "node_modules", ".venv", "venv",
            "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build", ".next",
            "target", ".idea", ".vscode", "coverage", ".tox",
        }
        top_dirs: list[str] = []
        top_files: list[str] = []
        by_extension: dict[str, int] = {}
        source_dirs: dict[str, int] = {}
        test_dirs: dict[str, int] = {}
        source_files = 0
        file_count = 0
        truncated = False
        try:
            root_entries = sorted(base.iterdir())
            top_dirs = [
                e.name for e in root_entries
                if e.is_dir() and not e.name.startswith(".") and e.name not in excluded_dirs
            ]
            top_files = [e.name for e in root_entries if e.is_file() and not e.name.startswith(".")]
        except OSError:
            pass
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in excluded_dirs and not d.startswith("."))
            rel_dir = os.path.relpath(dirpath, base).replace("\\", "/")
            for filename in sorted(filenames):
                file_count += 1
                if file_count > max_files:
                    truncated = True
                    break
                suffix = Path(filename).suffix.lower()
                by_extension[suffix or "<none>"] = by_extension.get(suffix or "<none>", 0) + 1
                if suffix in SOURCE_EXTENSIONS:
                    source_files += 1
                    key = rel_dir if rel_dir != "." else "<root>"
                    source_dirs[key] = source_dirs.get(key, 0) + 1
                    if "test" in key.lower() or filename.startswith("test_"):
                        test_dirs[key] = test_dirs.get(key, 0) + 1
            if truncated:
                break
        key_files = [
            name for name in top_files
            if any(_matches(name, pattern) for pattern in KEY_FILE_PATTERNS)
        ][:20]
        entrypoints = self._detect_entrypoints(base)
        return {
            "top_dirs": sorted(top_dirs)[:30],
            "top_files": sorted(top_files)[:30],
            "file_count": min(file_count, max_files),
            "by_extension": dict(sorted(by_extension.items(), key=lambda i: -i[1])[:20]),
            "key_files": key_files,
            "source_dirs": [{"path": k, "files": v} for k, v in sorted(source_dirs.items(), key=lambda i: -i[1])[:20]],
            "test_dirs": [{"path": k, "files": v} for k, v in sorted(test_dirs.items(), key=lambda i: -i[1])[:10]],
            "source_files": source_files,
            "entrypoints": entrypoints,
            "truncated": truncated,
        }

    @staticmethod
    def _detect_entrypoints(base: Path) -> list[str]:
        out: list[str] = []
        for candidate in ("src/main.py", "main.py", "app.py", "manage.py", "index.js",
                          "src/index.ts", "src/main.ts", "cmd/main.go", "main.go",
                          "src/main.rs", "Program.cs"):
            if (base / candidate).exists():
                out.append(candidate)
        if (base / "package.json").exists():
            out.append("package.json:scripts")
        if (base / "pyproject.toml").exists():
            out.append("pyproject.toml:[project.scripts]")
        return out[:10]


def _matches(name: str, pattern: str) -> bool:
    if "*" not in pattern:
        return name == pattern
    prefix = pattern.replace("*", "")
    return name.startswith(prefix)


def _count_statuses(tasks: list[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for task in tasks:
        out[task.status] = out.get(task.status, 0) + 1
    return out


__all__ = ["MemoryService"]
