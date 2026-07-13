"""Thin Celery wrapper around the graph runner."""

from __future__ import annotations

from orchestrator.workers.celery_app import celery_app


@celery_app.task(name="orchestrator.run_task")
def run_task_celery(task_id: str) -> None:
    from orchestrator.graph.runner import run_task

    run_task(task_id)


@celery_app.task(name="orchestrator.resume_task")
def resume_task_celery(task_id: str, decision: dict) -> None:
    from orchestrator.graph.runner import resume_task

    resume_task(task_id, decision)


@celery_app.task(name="orchestrator.replay_task")
def replay_task_celery(
    new_task_id: str,
    original_task_id: str,
    llm_call_id: str | None = None,
    response_text: str | None = None,
) -> None:
    from orchestrator.observability.replay import run_replay

    run_replay(new_task_id, original_task_id, llm_call_id, response_text)
