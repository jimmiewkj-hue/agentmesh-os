"""Append-only audit service."""

from __future__ import annotations

from typing import Any

from ..storage.repo import Store


class AuditService:
    def __init__(self, store: Store, project_id: str):
        self.store = store
        self.project_id = project_id

    def log(self, actor: str, action: str, subject: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.store.append_audit(self.project_id, actor, action, subject, payload)

    def list(self, *, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        return self.store.list_audit(self.project_id, limit=limit, offset=offset)

    def verify(self) -> dict[str, Any]:
        ok, errors = self.store.verify_audit_chain(self.project_id)
        return {"ok": ok, "errors": errors, "count": self.store.audit_count(self.project_id)}

    def replay(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        events = self.store.list_audit(self.project_id, limit=limit)
        return [
            {
                "seq": e["seq"], "ts": e["ts"], "actor": e["actor"], "action": e["action"],
                "subject": e["subject"], "payload": e.get("payload", {}),
            }
            for e in events
        ]
