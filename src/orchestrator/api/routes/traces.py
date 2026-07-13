"""Trace and cost endpoints (read side of observability)."""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException

from orchestrator.db.models import LLMCallRow, SpanRow, Task
from orchestrator.db.repo import DBLLMCallStore
from orchestrator.db.session import get_sessionmaker
from orchestrator.observability.cost import aggregate_costs, task_costs

router = APIRouter(prefix="/traces", tags=["traces"])


@router.get("/tasks")
def list_traced_tasks(limit: int = 25) -> dict:
    with get_sessionmaker()() as session:
        cost_subquery = (
            sa.select(LLMCallRow.task_id, sa.func.sum(LLMCallRow.cost_usd).label("cost"))
            .group_by(LLMCallRow.task_id)
            .subquery()
        )
        rows = session.execute(
            sa.select(Task, cost_subquery.c.cost)
            .join(cost_subquery, cost_subquery.c.task_id == Task.id, isouter=True)
            .order_by(Task.created_at.desc())
            .limit(limit)
        ).all()
        return {
            "tasks": [
                {
                    "task_id": task.id,
                    "status": task.status,
                    "request": task.request[:120],
                    "replay_of": task.replay_of,
                    "created_at": task.created_at.isoformat(),
                    "total_usd": round(float(cost or 0.0), 8),
                }
                for task, cost in rows
            ]
        }


@router.get("/aggregates/costs")
def cost_aggregates() -> dict:
    return aggregate_costs()


@router.get("/{task_id}")
def get_trace(task_id: str) -> dict:
    with get_sessionmaker()() as session:
        spans = session.scalars(
            sa.select(SpanRow).where(SpanRow.task_id == task_id).order_by(SpanRow.start_time)
        ).all()
        if not spans and session.get(Task, task_id) is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        return {
            "task_id": task_id,
            "spans": [
                {
                    "id": span.id,
                    "trace_id": span.trace_id,
                    "parent_id": span.parent_id,
                    "name": span.name,
                    "kind": span.kind,
                    "agent": span.agent,
                    "status": span.status,
                    "attributes": span.attributes,
                    "start_time": span.start_time.isoformat(),
                    "duration_ms": span.duration_ms,
                }
                for span in spans
            ],
            "llm_calls": DBLLMCallStore().for_task(task_id),
        }


@router.get("/{task_id}/costs")
def get_task_costs(task_id: str) -> dict:
    costs = task_costs(task_id)
    if costs is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return costs
