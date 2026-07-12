"""Conditional-edge functions and wave computation for the task graph."""

from __future__ import annotations

from langgraph.types import Send

from orchestrator.config import get_settings
from orchestrator.graph.state import TaskState
from orchestrator.planning.schemas import ExecutionPlan


def plan_gate(state: TaskState) -> str:
    """After planning: fail fast, escalate on low confidence, else execute."""
    if state.get("plan") is None:
        return "failed"
    if state.get("confidence", 1.0) < get_settings().plan_confidence_threshold:
        return "escalate"
    return "schedule"


def compute_wave(state: TaskState) -> list[dict]:
    """Payloads for every subtask that is ready to run now.

    Ready = not completed, all dependencies completed. Subtasks carrying a
    rework/failed_attempt status are re-dispatched with feedback; escalation
    for exhausted retries is decided in `gather` before this runs again.
    """
    plan = ExecutionPlan.model_validate(state["plan"])
    results = state.get("subtask_results", {})
    completed = {sid for sid, r in results.items() if r.get("status") == "completed"}

    wave = []
    for subtask in plan.subtasks:
        if subtask.id in completed:
            continue
        if not all(dep in completed for dep in subtask.depends_on):
            continue
        prior = results.get(subtask.id)
        wave.append(
            {
                "task_id": state["task_id"],
                "spec": subtask.model_dump(),
                "inputs": {dep: results[dep].get("output", "") for dep in subtask.depends_on},
                "feedback": prior.get("feedback") if prior else None,
                "prior": {
                    "error_count": prior.get("error_count", 0) if prior else 0,
                    "rework_count": prior.get("rework_count", 0) if prior else 0,
                    "attempts": prior.get("attempts", 0) if prior else 0,
                },
            }
        )
    return wave


def dispatch(state: TaskState) -> list[Send] | str:
    """Fan out the current wave to specialist executions, or synthesize."""
    wave = state.get("current_wave") or []
    if wave:
        return [Send("execute", payload) for payload in wave]
    return "synthesize"


def after_gather(state: TaskState) -> str:
    return "escalate" if state.get("needs_escalation") else "schedule"
