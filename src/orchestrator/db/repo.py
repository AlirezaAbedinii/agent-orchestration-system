"""Persistence repositories used by the graph nodes and API.

`DBTaskRepo` / `DBInvocationStore` are the real implementations;
`InMemoryTaskRepo` lets graph unit tests run without Postgres.
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.db.models import (
    LLMCallRow,
    MemoryEvent,
    PlanRow,
    SubtaskRow,
    Task,
    ToolInvocation,
)
from orchestrator.db.session import get_sessionmaker
from orchestrator.planning.schemas import ExecutionPlan
from orchestrator.tools.base import InvocationRecord


class DBInvocationStore:
    def __init__(self, session_factory: sessionmaker[Session] | None = None):
        self._sessions = session_factory or get_sessionmaker()

    def record(self, record: InvocationRecord) -> None:
        with self._sessions() as session, session.begin():
            session.add(
                ToolInvocation(
                    task_id=record.task_id,
                    subtask_sid=record.subtask_id,
                    specialist=record.specialist,
                    tool_name=record.tool_name,
                    arguments=record.arguments,
                    output=record.output,
                    status=record.status,
                    error=record.error,
                    latency_ms=record.latency_ms,
                    sensitive=record.sensitive,
                )
            )

    def count_executions(self, task_id: str, tool_name: str) -> int:
        with self._sessions() as session:
            return session.scalar(
                sa.select(sa.func.count())
                .select_from(ToolInvocation)
                .where(
                    ToolInvocation.task_id == task_id,
                    ToolInvocation.tool_name == tool_name,
                    ToolInvocation.status.in_(("success", "failure")),
                )
            )


class DBLLMCallStore:
    """Durable record of every LLM call (prompt, response, usage, cost)."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None):
        self._sessions = session_factory or get_sessionmaker()

    def record(self, **fields) -> None:
        with self._sessions() as session, session.begin():
            session.add(LLMCallRow(**fields))

    @staticmethod
    def _to_dict(row: LLMCallRow) -> dict:
        return {
            "id": row.id,
            "span_id": row.span_id,
            "task_id": row.task_id,
            "agent": row.agent,
            "model": row.model,
            "prompt": row.prompt,
            "response": row.response,
            "prompt_tokens": row.prompt_tokens,
            "completion_tokens": row.completion_tokens,
            "cost_usd": row.cost_usd,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    def for_task(self, task_id: str) -> list[dict]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(LLMCallRow)
                .where(LLMCallRow.task_id == task_id)
                .order_by(LLMCallRow.created_at, LLMCallRow.id)
            )
            return [self._to_dict(row) for row in rows]

    def get(self, llm_call_id: str) -> dict | None:
        with self._sessions() as session:
            row = session.get(LLMCallRow, llm_call_id)
            return self._to_dict(row) if row else None


class MemoryEventStore:
    """Audit log for long-term memory activity."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None):
        self._sessions = session_factory or get_sessionmaker()

    def record(
        self,
        *,
        user_id: str,
        memory_id: str,
        kind: str,
        action: str,
        task_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        with self._sessions() as session, session.begin():
            session.add(
                MemoryEvent(
                    user_id=user_id,
                    memory_id=memory_id,
                    kind=kind,
                    action=action,
                    task_id=task_id,
                    detail=detail,
                )
            )

    def recent(self, user_id: str, limit: int = 20) -> list[dict]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(MemoryEvent)
                .where(MemoryEvent.user_id == user_id)
                .order_by(MemoryEvent.created_at.desc())
                .limit(limit)
            )
            return [
                {
                    "memory_id": row.memory_id,
                    "kind": row.kind,
                    "action": row.action,
                    "task_id": row.task_id,
                    "detail": row.detail,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    def purge_user(self, user_id: str) -> int:
        with self._sessions() as session, session.begin():
            result = session.execute(
                sa.delete(MemoryEvent).where(MemoryEvent.user_id == user_id)
            )
            return result.rowcount or 0


class DBTaskRepo:
    def __init__(self, session_factory: sessionmaker[Session] | None = None):
        self._sessions = session_factory or get_sessionmaker()

    def create_task(
        self,
        request: str,
        user_id: str = "default",
        require_human_review: bool = False,
        replay_of: str | None = None,
    ) -> str:
        with self._sessions() as session, session.begin():
            task = Task(
                request=request,
                user_id=user_id,
                require_human_review=require_human_review,
                replay_of=replay_of,
            )
            session.add(task)
            session.flush()
            return task.id

    def set_status(self, task_id: str, status: str, error: str | None = None) -> None:
        with self._sessions() as session, session.begin():
            task = session.get(Task, task_id)
            if task is None:
                return
            task.status = status
            if error is not None:
                task.error = error
            if status in ("completed", "failed", "escalated"):
                task.completed_at = datetime.now(timezone.utc)

    def set_final_output(self, task_id: str, output: str) -> None:
        with self._sessions() as session, session.begin():
            task = session.get(Task, task_id)
            if task is not None:
                task.final_output = output
                task.status = "completed"
                task.completed_at = datetime.now(timezone.utc)

    def save_plan(self, task_id: str, plan: ExecutionPlan) -> str:
        with self._sessions() as session, session.begin():
            version = (
                session.scalar(
                    sa.select(sa.func.count()).select_from(PlanRow).where(PlanRow.task_id == task_id)
                )
                + 1
            )
            row = PlanRow(
                task_id=task_id, version=version, confidence=plan.confidence, raw=plan.model_dump()
            )
            session.add(row)
            session.flush()
            for subtask in plan.subtasks:
                session.add(
                    SubtaskRow(
                        task_id=task_id,
                        plan_id=row.id,
                        sid=subtask.id,
                        description=subtask.description,
                        specialist=subtask.specialist,
                        depends_on=subtask.depends_on,
                        expected_output_format=subtask.expected_output_format,
                        estimated_complexity=subtask.estimated_complexity,
                    )
                )
            return row.id

    def record_subtask(self, task_id: str, sid: str, **fields) -> None:
        """Update the subtask row (of the latest plan) after an attempt."""
        with self._sessions() as session, session.begin():
            latest_plan_id = session.scalar(
                sa.select(PlanRow.id)
                .where(PlanRow.task_id == task_id)
                .order_by(PlanRow.version.desc())
                .limit(1)
            )
            row = session.scalar(
                sa.select(SubtaskRow).where(
                    SubtaskRow.plan_id == latest_plan_id, SubtaskRow.sid == sid
                )
            )
            if row is None:
                return
            for key, value in fields.items():
                setattr(row, key, value)

    def get_task(self, task_id: str) -> dict | None:
        with self._sessions() as session:
            task = session.get(Task, task_id)
            if task is None:
                return None
            plan = session.scalar(
                sa.select(PlanRow).where(PlanRow.task_id == task_id).order_by(PlanRow.version.desc()).limit(1)
            )
            subtasks = []
            if plan is not None:
                subtasks = [
                    {
                        "sid": s.sid,
                        "description": s.description,
                        "specialist": s.specialist,
                        "depends_on": s.depends_on,
                        "status": s.status,
                        "attempts": s.attempts,
                        "output": s.output,
                        "review_score": s.review_score,
                        "review_feedback": s.review_feedback,
                        "error": s.error,
                    }
                    for s in session.scalars(
                        sa.select(SubtaskRow).where(SubtaskRow.plan_id == plan.id).order_by(SubtaskRow.sid)
                    )
                ]
            return {
                "task_id": task.id,
                "request": task.request,
                "user_id": task.user_id,
                "status": task.status,
                "require_human_review": task.require_human_review,
                "replay_of": task.replay_of,
                "plan": plan.raw if plan is not None else None,
                "confidence": plan.confidence if plan is not None else None,
                "subtasks": subtasks,
                "final_output": task.final_output,
                "error": task.error,
            }


class InMemoryTaskRepo:
    """Dict-backed repo for graph unit tests (no Postgres required)."""

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.plans: dict[str, list[dict]] = {}
        self.subtasks: dict[str, dict[str, dict]] = {}

    def create_task(self, request: str, user_id: str = "default", require_human_review: bool = False) -> str:
        task_id = f"task-{len(self.tasks) + 1}"
        self.tasks[task_id] = {
            "task_id": task_id,
            "request": request,
            "user_id": user_id,
            "status": "pending",
            "require_human_review": require_human_review,
            "final_output": None,
            "error": None,
        }
        return task_id

    def set_status(self, task_id: str, status: str, error: str | None = None) -> None:
        task = self.tasks.setdefault(task_id, {"task_id": task_id})
        task["status"] = status
        if error is not None:
            task["error"] = error

    def set_final_output(self, task_id: str, output: str) -> None:
        task = self.tasks.setdefault(task_id, {"task_id": task_id})
        task["final_output"] = output
        task["status"] = "completed"

    def save_plan(self, task_id: str, plan: ExecutionPlan) -> str:
        self.plans.setdefault(task_id, []).append(plan.model_dump())
        self.subtasks[task_id] = {
            s.id: {"sid": s.id, "status": "pending", "attempts": 0} for s in plan.subtasks
        }
        return f"plan-{len(self.plans[task_id])}"

    def record_subtask(self, task_id: str, sid: str, **fields) -> None:
        self.subtasks.setdefault(task_id, {}).setdefault(sid, {"sid": sid})
        self.subtasks[task_id][sid].update(fields)

    def get_task(self, task_id: str) -> dict | None:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        plans = self.plans.get(task_id, [])
        return {
            **task,
            "plan": plans[-1] if plans else None,
            "subtasks": list(self.subtasks.get(task_id, {}).values()),
        }
