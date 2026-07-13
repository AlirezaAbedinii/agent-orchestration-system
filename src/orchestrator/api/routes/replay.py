"""Replay endpoints: step listing, replay/fork launch, side-by-side compare."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, model_validator

from orchestrator.config import get_settings
from orchestrator.db.repo import DBLLMCallStore, DBTaskRepo
from orchestrator.observability.replay import compare, create_replay_task, run_replay

router = APIRouter(prefix="/replay", tags=["replay"])


class ReplayRequest(BaseModel):
    llm_call_id: str | None = None
    response_text: str | None = None

    @model_validator(mode="after")
    def _both_or_neither(self) -> "ReplayRequest":
        if (self.llm_call_id is None) != (self.response_text is None):
            raise ValueError("Provide both llm_call_id and response_text to fork, or neither to replay")
        return self


@router.get("/{task_id}/steps")
def list_steps(task_id: str) -> dict:
    if DBTaskRepo().get_task(task_id) is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return {
        "task_id": task_id,
        "steps": [
            {
                "id": call["id"],
                "agent": call["agent"],
                "model": call["model"],
                "prompt": call["prompt"][:200],
                "response": call["response"][:200],
                "prompt_tokens": call["prompt_tokens"],
                "completion_tokens": call["completion_tokens"],
                "created_at": call["created_at"],
            }
            for call in DBLLMCallStore().for_task(task_id)
        ],
    }


@router.post("/{task_id}")
def launch_replay(task_id: str, body: ReplayRequest, background_tasks: BackgroundTasks) -> dict:
    if DBTaskRepo().get_task(task_id) is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if body.llm_call_id is not None:
        call = DBLLMCallStore().get(body.llm_call_id)
        if call is None or call["task_id"] != task_id:
            raise HTTPException(
                status_code=404, detail=f"LLM call {body.llm_call_id} not found in task {task_id}"
            )

    replay_task_id, mode = create_replay_task(task_id, body.llm_call_id)
    if get_settings().run_mode == "inline":
        background_tasks.add_task(
            run_replay, replay_task_id, task_id, body.llm_call_id, body.response_text
        )
    else:
        from orchestrator.workers.run_task import replay_task_celery

        replay_task_celery.delay(replay_task_id, task_id, body.llm_call_id, body.response_text)
    return {"replay_task_id": replay_task_id, "mode": mode, "replay_of": task_id}


@router.get("/{fork_task_id}/compare")
def compare_replay(fork_task_id: str) -> dict:
    try:
        return compare(fork_task_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
