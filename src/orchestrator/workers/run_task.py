"""Thin Celery wrapper around the graph runner."""

from __future__ import annotations

from orchestrator.workers.celery_app import celery_app


@celery_app.task(name="orchestrator.run_task")
def run_task_celery(task_id: str) -> None:
    from orchestrator.graph.runner import run_task

    run_task(task_id)
