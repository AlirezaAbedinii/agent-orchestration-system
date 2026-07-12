"""Approval queue endpoints: list, detail, resolve (with graph resume), chat."""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, ValidationError

from orchestrator.config import get_settings
from orchestrator.hitl.queue import ApprovalQueue
from orchestrator.llm.clients import get_llm_client
from orchestrator.memory.working import WorkingMemory
from orchestrator.planning.schemas import ExecutionPlan

router = APIRouter(prefix="/approvals", tags=["approvals"])

# Stable marker; mock fixtures match on it.
HITL_CHAT_MARKER = "Answer the reviewer's question"

CHAT_PROMPT = """{marker} about a task that is paused for human approval.
Ground your answer ONLY in the context below; if it is not in the context, say so.

Decision point: {reasoning}
Proposed action: {proposed_action}

Task request: {request}

Plan:
{plan}

Subtask states:
{subtask_states}

Working memory snapshot:
{working}

Reviewer's question: {question}
"""


class ResolveRequest(BaseModel):
    action: Literal["approve", "modify", "reject", "take_over"]
    payload: dict | None = None
    notes: str = ""


class ChatRequest(BaseModel):
    question: str


def _validate_resolution(approval: dict, body: ResolveRequest) -> None:
    """Reject malformed human input up front so the approval stays pending."""
    gate = approval["gate_key"]
    payload = body.payload or {}
    if body.action == "modify" and gate == "plan":
        try:
            ExecutionPlan.model_validate(payload.get("plan"))
        except ValidationError as error:
            raise HTTPException(status_code=422, detail=f"Modified plan is invalid: {error}") from error
    if body.action == "take_over":
        if gate in ("plan", "final") and not payload.get("final_output"):
            raise HTTPException(status_code=422, detail="take_over requires payload.final_output")
        if gate.startswith("subtask:") and not payload.get("output"):
            raise HTTPException(status_code=422, detail="take_over requires payload.output")
    if body.action == "modify":
        if gate == "final" and not payload.get("final_output"):
            raise HTTPException(status_code=422, detail="modify requires payload.final_output")
        if gate.startswith("tool:") and not payload.get("arguments"):
            raise HTTPException(status_code=422, detail="modify requires payload.arguments")


@router.get("")
def list_approvals(status: str | None = None, task_id: str | None = None) -> dict:
    return {"approvals": ApprovalQueue().list(status=status, task_id=task_id)}


@router.get("/{approval_id}")
def get_approval(approval_id: str) -> dict:
    approval = ApprovalQueue().get(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail=f"Approval {approval_id} not found")
    return approval


@router.post("/{approval_id}/resolve")
def resolve_approval(
    approval_id: str, body: ResolveRequest, background_tasks: BackgroundTasks
) -> dict:
    queue = ApprovalQueue()
    approval = queue.get(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail=f"Approval {approval_id} not found")
    if approval["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Approval is {approval['status']}, not pending")

    _validate_resolution(approval, body)
    resolved = queue.resolve(approval_id, action=body.action, payload=body.payload, notes=body.notes)
    decision = {"action": body.action, "payload": body.payload or {}, "notes": body.notes}

    if get_settings().run_mode == "inline":
        from orchestrator.graph.runner import resume_task

        background_tasks.add_task(resume_task, approval["task_id"], decision)
    else:
        from orchestrator.workers.run_task import resume_task_celery

        resume_task_celery.delay(approval["task_id"], decision)
    return {"approval": resolved, "resumed": True}


@router.post("/{approval_id}/chat")
def chat_about_approval(approval_id: str, body: ChatRequest) -> dict:
    """Clarifying questions for the paused task, grounded in checkpointed
    state, the approval's decision context, and working memory."""
    approval = ApprovalQueue().get(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail=f"Approval {approval_id} not found")

    from orchestrator.graph.runner import get_production_graph

    task_id = approval["task_id"]
    snapshot = get_production_graph().get_state({"configurable": {"thread_id": task_id}})
    values = snapshot.values or {}
    context = approval.get("context") or {}

    prompt = CHAT_PROMPT.format(
        marker=HITL_CHAT_MARKER,
        reasoning=approval.get("reasoning"),
        proposed_action=json.dumps(approval.get("proposed_action"))[:1500],
        request=values.get("request") or context.get("task", {}).get("request", ""),
        plan=json.dumps(values.get("plan") or context.get("plan"))[:3000],
        subtask_states=json.dumps(context.get("subtask_states", {}))[:1500],
        working=json.dumps(WorkingMemory().snapshot(task_id))[:2000],
        question=body.question,
    )
    answer = get_llm_client().complete("hitl", prompt).text
    return {"approval_id": approval_id, "question": body.question, "answer": answer}
