"""Task intake and status endpoints."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from orchestrator.config import get_settings
from orchestrator.db.repo import DBTaskRepo

router = APIRouter(prefix="/tasks", tags=["tasks"])


class CreateTaskRequest(BaseModel):
    request: str = Field(min_length=1)
    user_id: str = "default"
    require_human_review: bool = False


class CreateTaskResponse(BaseModel):
    task_id: str
    status: str


@router.post("", response_model=CreateTaskResponse, status_code=202)
def create_task(body: CreateTaskRequest, background_tasks: BackgroundTasks) -> CreateTaskResponse:
    repo = DBTaskRepo()
    task_id = repo.create_task(
        request=body.request,
        user_id=body.user_id,
        require_human_review=body.require_human_review,
    )
    if get_settings().run_mode == "inline":
        from orchestrator.graph.runner import run_task

        background_tasks.add_task(run_task, task_id)
    else:
        from orchestrator.workers.run_task import run_task_celery

        run_task_celery.delay(task_id)
    return CreateTaskResponse(task_id=task_id, status="pending")


@router.get("/{task_id}")
def get_task(task_id: str) -> dict:
    bundle = DBTaskRepo().get_task(task_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return bundle
