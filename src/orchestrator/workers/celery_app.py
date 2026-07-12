"""Celery application (async run mode; fully exercised in Phase 5)."""

from __future__ import annotations

from celery import Celery

from orchestrator.config import get_settings

settings = get_settings()

celery_app = Celery(
    "orchestrator",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["orchestrator.workers.run_task"],
)
celery_app.conf.task_track_started = True
