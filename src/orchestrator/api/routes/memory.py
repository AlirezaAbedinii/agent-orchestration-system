"""Memory endpoints: per-user dashboard, user-data deletion, maintenance."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from orchestrator.db.repo import MemoryEventStore
from orchestrator.llm.clients import get_llm_client
from orchestrator.memory.longterm import LongTermMemory
from orchestrator.memory.management import consolidate, expire
from orchestrator.memory.working import WorkingMemory

router = APIRouter(prefix="/memory", tags=["memory"])


class MaintenanceRequest(BaseModel):
    user_id: str | None = None


@router.get("/users/{user_id}")
def memory_dashboard(user_id: str) -> dict:
    """What the system remembers about a user, plus recent memory activity."""
    collections = LongTermMemory().get_all(user_id)
    return {
        "user_id": user_id,
        "counts": {kind: len(items) for kind, items in collections.items()},
        "collections": collections,
        "recent_events": MemoryEventStore().recent(user_id, limit=25),
    }


@router.delete("/users/{user_id}")
def delete_user_memory(user_id: str) -> dict:
    """Purge everything remembered about a user (long-term, working, audit)."""
    chroma_deleted = LongTermMemory().delete_user(user_id)
    working_cleared = WorkingMemory().clear_user(user_id)
    events_purged = MemoryEventStore().purge_user(user_id)
    return {
        "user_id": user_id,
        "deleted": {
            "long_term": chroma_deleted,
            "working_memory_tasks": working_cleared,
            "audit_events": events_purged,
        },
    }


@router.post("/maintenance/consolidate")
def run_consolidation(body: MaintenanceRequest) -> dict:
    return consolidate(
        LongTermMemory(), get_llm_client(), user_id=body.user_id, events=MemoryEventStore()
    )


@router.post("/maintenance/expire")
def run_expiration(body: MaintenanceRequest) -> dict:
    return expire(LongTermMemory(), user_id=body.user_id, events=MemoryEventStore())
