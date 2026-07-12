"""Scheduled memory maintenance (Celery beat) — consolidation and expiration.

The same operations are manually triggerable via POST /memory/maintenance/*.
"""

from __future__ import annotations

from celery.schedules import crontab

from orchestrator.workers.celery_app import celery_app


def _deps():
    from orchestrator.db.repo import MemoryEventStore
    from orchestrator.llm.clients import get_llm_client
    from orchestrator.memory.longterm import LongTermMemory

    return LongTermMemory(), get_llm_client(), MemoryEventStore()


@celery_app.task(name="orchestrator.memory.consolidate")
def consolidate_memories(user_id: str | None = None) -> dict:
    from orchestrator.memory.management import consolidate

    longterm, llm, events = _deps()
    return consolidate(longterm, llm, user_id=user_id, events=events)


@celery_app.task(name="orchestrator.memory.expire")
def expire_memories(user_id: str | None = None) -> dict:
    from orchestrator.memory.management import expire

    longterm, _, events = _deps()
    return expire(longterm, user_id=user_id, events=events)


celery_app.conf.beat_schedule = {
    "memory-consolidation-daily": {
        "task": "orchestrator.memory.consolidate",
        "schedule": crontab(hour=3, minute=0),
    },
    "memory-expiration-daily": {
        "task": "orchestrator.memory.expire",
        "schedule": crontab(hour=3, minute=30),
    },
}
