"""Session import service: reader dispatch, normalization and archival."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ValidationError
from ..models import AgentRecord, ConversationRecord, Project
from ..storage.repo import Store
from ..util import atomic_write_text, ensure_dir, new_id, now_iso
from . import dsh, opencode, qoder, trae, workbuddy, zcode
from .common import ParsedSession, summarize

READERS = {
    "qoder": qoder,
    "zcode": zcode,
    "workbuddy": workbuddy,
    "dsh": dsh,
    "opencode": opencode,
    "trae": trae,
}


@dataclass
class ImportResult:
    agent_id: str
    imported: int
    skipped: int
    records: list[ConversationRecord]

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id, "imported": self.imported, "skipped": self.skipped,
            "records": [r.to_dict() for r in self.records],
        }


class SessionImportService:
    def __init__(self, store: Store, project: Project):
        self.store = store
        self.project = project

    def import_for_agent(self, agent: AgentRecord, *, limit: int = 20,
                         include_unscoped: bool = False) -> ImportResult:
        reader_name = str(agent.adapter_config.get("session_reader") or agent.agent_id)
        data_path = str(agent.adapter_config.get("data_path") or "")
        if reader_name not in READERS:
            raise ValidationError(f"Agent {agent.agent_id} 没有会话读取器（{reader_name}）")
        if not data_path:
            raise ValidationError(f"Agent {agent.agent_id} 未配置会话存储路径")
        reader = READERS[reader_name]
        scan_limit = max(limit * 5, 50)
        kwargs: dict[str, Any] = {"project_root": "", "limit": scan_limit}
        if reader_name == "trae":
            kwargs["cache_dir"] = Path(self.project.root) / ".agentloop" / "cache"
        sessions = list(reader.iter_sessions(Path(data_path), **kwargs))

        records: list[ConversationRecord] = []
        skipped = 0
        for session in sessions:
            if len(records) >= limit:
                break
            if not include_unscoped and not self._scope_ok(session):
                skipped += 1
                continue
            record = self._archive(agent, session)
            records.append(record)
        self.store.append_audit(
            self.project.project_id, "session-importer", "sessions.imported",
            subject=agent.agent_id,
            payload={"imported": len(records), "skipped": skipped, "limit": limit},
        )
        return ImportResult(agent.agent_id, len(records), skipped, records)

    def _scope_ok(self, session: ParsedSession) -> bool:
        if session.cwd:
            from .common import project_matches
            return project_matches(session.cwd, self.project.root)
        # ZCode rollouts do not record a cwd. Keep sessions whose recorded text
        # mentions the project root; never import unrelated history silently.
        needle = self.project.root.replace("\\", "/").lower()
        haystack = "\n".join(
            m.text + " " + json.dumps(m.tool_calls, ensure_ascii=False)
            for m in session.messages
        ).replace("\\", "/").lower()
        return needle in haystack

    def _archive(self, agent: AgentRecord, session: ParsedSession) -> ConversationRecord:
        target_dir = ensure_dir(Path(self.project.root) / ".agentloop" / "conversations" / agent.agent_id)
        target = target_dir / f"{session.session_id.replace('/', '_')}.json"
        summary = summarize(session)
        atomic_write_text(target, json.dumps({
            "session": {
                "id": session.session_id, "source": session.source_path, "title": session.title,
                "cwd": session.cwd, "model": session.model,
                "started_at": session.started_at, "ended_at": session.ended_at,
                "token_usage": session.token_usage,
            },
            "summary": summary,
            "messages": [m.to_dict() for m in session.messages],
        }, ensure_ascii=False, indent=2))
        existing = self.store.get_conversation(self.project.project_id, agent.agent_id, session.session_id)
        record = ConversationRecord(
            conversation_id=(existing["conversation_id"] if existing else new_id("conv")),
            project_id=self.project.project_id,
            agent_id=agent.agent_id,
            session_id=session.session_id,
            source_path=session.source_path,
            title=session.title,
            cwd=session.cwd,
            started_at=session.started_at,
            ended_at=session.ended_at,
            message_count=len(session.messages),
            token_usage=session.token_usage,
            summary=summary,
            raw_path=str(target),
            imported_at=now_iso(),
            fingerprint=session.fingerprint,
        )
        self.store.upsert_conversation(record)
        return record

    def recent_context(self, *, limit: int = 5) -> list[dict[str, Any]]:
        rows = self.store.list_conversations(self.project.project_id, limit=limit)
        return [
            {
                "agent_id": row["agent_id"], "session_id": row["session_id"],
                "title": row["title"], "summary": row["summary"],
                "token_usage": row["token_usage"], "imported_at": row["imported_at"],
            }
            for row in rows
        ]
