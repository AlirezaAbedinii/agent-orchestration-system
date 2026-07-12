"""Approval queue: durable escalation records with full decision context.

`ensure` is idempotent per (task_id, gate_key) because interrupted graph nodes
re-execute their pre-interrupt code on resume — the second pass must find the
existing row instead of enqueueing (and notifying) twice.
`InMemoryApprovalQueue` is the unit-test drop-in.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.db.models import ApprovalRow
from orchestrator.db.session import get_sessionmaker
from orchestrator.hitl.notify import notify_approval


def _row_to_dict(row: ApprovalRow) -> dict:
    return {
        "id": row.id,
        "task_id": row.task_id,
        "gate_key": row.gate_key,
        "trigger": row.trigger,
        "level": row.level,
        "status": row.status,
        "context": row.context,
        "proposed_action": row.proposed_action,
        "reasoning": row.reasoning,
        "resolution_action": row.resolution_action,
        "resolution_payload": row.resolution_payload,
        "resolution_notes": row.resolution_notes,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "review_seconds": row.review_seconds,
    }


class ApprovalQueue:
    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        notifier: Callable[[dict], None] | None = None,
    ):
        self._sessions = session_factory or get_sessionmaker()
        self._notify = notifier or notify_approval

    def ensure(
        self,
        *,
        task_id: str,
        gate_key: str,
        trigger: str,
        level: str,
        context: dict,
        proposed_action: dict | None,
        reasoning: str,
    ) -> tuple[str, bool]:
        """Create the approval once; on re-execution return the existing row."""
        with self._sessions() as session, session.begin():
            existing = session.scalar(
                sa.select(ApprovalRow).where(
                    ApprovalRow.task_id == task_id, ApprovalRow.gate_key == gate_key
                )
            )
            if existing is not None:
                return existing.id, False
            row = ApprovalRow(
                task_id=task_id,
                gate_key=gate_key,
                trigger=trigger,
                level=level,
                status="pending",
                context=context,
                proposed_action=proposed_action,
                reasoning=reasoning,
            )
            session.add(row)
            session.flush()
            approval = _row_to_dict(row)
        self._notify(approval)
        return approval["id"], True

    def record_notify(
        self,
        *,
        task_id: str,
        gate_key: str,
        trigger: str,
        context: dict,
        proposed_action: dict | None,
        reasoning: str,
    ) -> str:
        """NOTIFY level: record and inform, execution proceeds without pausing."""
        with self._sessions() as session, session.begin():
            existing = session.scalar(
                sa.select(ApprovalRow).where(
                    ApprovalRow.task_id == task_id, ApprovalRow.gate_key == gate_key
                )
            )
            if existing is not None:
                return existing.id
            row = ApprovalRow(
                task_id=task_id,
                gate_key=gate_key,
                trigger=trigger,
                level="notify",
                status="notified",
                context=context,
                proposed_action=proposed_action,
                reasoning=reasoning,
                resolved_at=datetime.now(timezone.utc),
            )
            session.add(row)
            session.flush()
            approval = _row_to_dict(row)
        self._notify(approval)
        return approval["id"]

    def get(self, approval_id: str) -> dict | None:
        with self._sessions() as session:
            row = session.get(ApprovalRow, approval_id)
            return _row_to_dict(row) if row else None

    def list(self, status: str | None = None, task_id: str | None = None) -> list[dict]:
        with self._sessions() as session:
            query = sa.select(ApprovalRow).order_by(ApprovalRow.created_at.desc())
            if status:
                query = query.where(ApprovalRow.status == status)
            if task_id:
                query = query.where(ApprovalRow.task_id == task_id)
            return [_row_to_dict(row) for row in session.scalars(query)]

    def resolve(
        self, approval_id: str, *, action: str, payload: dict | None = None, notes: str = ""
    ) -> dict:
        with self._sessions() as session, session.begin():
            row = session.get(ApprovalRow, approval_id)
            if row is None:
                raise KeyError(f"Approval {approval_id} not found")
            if row.status != "pending":
                raise ValueError(f"Approval {approval_id} is {row.status}, not pending")
            now = datetime.now(timezone.utc)
            row.status = "resolved"
            row.resolution_action = action
            row.resolution_payload = payload
            row.resolution_notes = notes
            row.resolved_at = now
            row.review_seconds = (now - row.created_at).total_seconds()
            return _row_to_dict(row)


class InMemoryApprovalQueue:
    """Dict-backed drop-in for unit tests."""

    def __init__(self, notifier: Callable[[dict], None] | None = None):
        self._rows: dict[str, dict] = {}
        self._notify = notifier or (lambda approval: None)

    def _find(self, task_id: str, gate_key: str) -> dict | None:
        return next(
            (r for r in self._rows.values() if r["task_id"] == task_id and r["gate_key"] == gate_key),
            None,
        )

    def ensure(self, *, task_id, gate_key, trigger, level, context, proposed_action, reasoning):
        existing = self._find(task_id, gate_key)
        if existing is not None:
            return existing["id"], False
        approval = {
            "id": uuid.uuid4().hex,
            "task_id": task_id,
            "gate_key": gate_key,
            "trigger": trigger,
            "level": level,
            "status": "pending",
            "context": context,
            "proposed_action": proposed_action,
            "reasoning": reasoning,
            "resolution_action": None,
            "resolution_payload": None,
            "resolution_notes": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "resolved_at": None,
            "review_seconds": None,
        }
        self._rows[approval["id"]] = approval
        self._notify(approval)
        return approval["id"], True

    def record_notify(self, *, task_id, gate_key, trigger, context, proposed_action, reasoning):
        existing = self._find(task_id, gate_key)
        if existing is not None:
            return existing["id"]
        approval_id, _ = self.ensure(
            task_id=task_id, gate_key=gate_key, trigger=trigger, level="notify",
            context=context, proposed_action=proposed_action, reasoning=reasoning,
        )
        self._rows[approval_id]["status"] = "notified"
        self._rows[approval_id]["resolved_at"] = datetime.now(timezone.utc).isoformat()
        return approval_id

    def get(self, approval_id):
        return self._rows.get(approval_id)

    def list(self, status=None, task_id=None):
        rows = list(self._rows.values())
        if status:
            rows = [r for r in rows if r["status"] == status]
        if task_id:
            rows = [r for r in rows if r["task_id"] == task_id]
        return rows

    def resolve(self, approval_id, *, action, payload=None, notes=""):
        row = self._rows[approval_id]
        if row["status"] != "pending":
            raise ValueError(f"Approval {approval_id} is {row['status']}, not pending")
        created = datetime.fromisoformat(row["created_at"])
        now = datetime.now(timezone.utc)
        row.update(
            status="resolved",
            resolution_action=action,
            resolution_payload=payload,
            resolution_notes=notes,
            resolved_at=now.isoformat(),
            review_seconds=(now - created).total_seconds(),
        )
        return row
