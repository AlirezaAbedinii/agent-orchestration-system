"""Shared LangGraph state for a task execution.

`subtask_results` uses a dict-merge reducer so parallel specialist branches
(fan-out via Send) can each contribute their subtask's result; `dispatch_log`
accumulates the wave batches the scheduler dispatched (used to assert parallel
batching in tests and, later, in traces).
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


def merge_results(left: dict | None, right: dict | None) -> dict:
    return {**(left or {}), **(right or {})}


class TaskState(TypedDict, total=False):
    task_id: str
    request: str
    user_id: str
    require_human_review: bool
    plan: dict | None
    plan_error: str | None
    confidence: float
    retrieved_memory_ids: list[str]
    subtask_results: Annotated[dict[str, dict], merge_results]
    dispatch_log: Annotated[list[list[str]], operator.add]
    current_wave: list[dict]
    needs_escalation: bool
    escalation_reason: str | None
    final_output: str | None
