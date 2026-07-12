"""SQLAlchemy ORM models: tasks, plans, subtasks, tool invocations (Phase 1)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    request: Mapped[str] = mapped_column(Text)
    user_id: Mapped[str] = mapped_column(String(64), default="default")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    require_human_review: Mapped[bool] = mapped_column(Boolean, default=False)
    final_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlanRow(Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    confidence: Mapped[float] = mapped_column(Float)
    raw: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SubtaskRow(Base):
    __tablename__ = "subtasks"
    __table_args__ = (UniqueConstraint("plan_id", "sid", name="uq_subtasks_plan_sid"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), index=True)
    sid: Mapped[str] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(Text)
    specialist: Mapped[str] = mapped_column(String(16))
    depends_on: Mapped[list] = mapped_column(JSON, default=list)
    expected_output_format: Mapped[str] = mapped_column(Text, default="plain text")
    estimated_complexity: Mapped[str] = mapped_column(String(8), default="medium")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    review_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ApprovalRow(Base):
    """Human-in-the-loop approval queue: one row per escalation decision point."""

    __tablename__ = "approvals"
    __table_args__ = (UniqueConstraint("task_id", "gate_key", name="uq_approvals_task_gate"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    gate_key: Mapped[str] = mapped_column(String(64))
    trigger: Mapped[str] = mapped_column(String(32))
    level: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|resolved|notified
    context: Mapped[dict] = mapped_column(JSON)
    proposed_action: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    resolution_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)


class MemoryEvent(Base):
    """Audit log of long-term memory activity (created/retrieved/consolidated/
    expired/deleted) — feeds the dashboard and, later, tracing."""

    __tablename__ = "memory_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    memory_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ToolInvocation(Base):
    __tablename__ = "tool_invocations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    subtask_sid: Mapped[str | None] = mapped_column(String(16), nullable=True)
    specialist: Mapped[str] = mapped_column(String(16))
    tool_name: Mapped[str] = mapped_column(String(32), index=True)
    arguments: Mapped[dict] = mapped_column(JSON)
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
