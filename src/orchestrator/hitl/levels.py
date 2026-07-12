"""Granular approval levels and the trigger → level mapping.

Not every escalation needs the same review depth:
  NOTIFY         — proceed, but inform the human (never pauses)
  APPROVE_ACTION — human confirms the next step before it runs
  APPROVE_PLAN   — human reviews the full execution plan before any work
  TAKE_OVER      — human provides the output directly; agents stand down

The default mapping follows the build plan; it can be overridden per trigger
via APPROVAL_LEVEL_OVERRIDES (e.g. '{"sensitive_operation": "notify"}').
"""

from __future__ import annotations

from enum import Enum

from orchestrator.config import get_settings
from orchestrator.hitl.triggers import Trigger


class ApprovalLevel(str, Enum):
    NOTIFY = "notify"
    APPROVE_ACTION = "approve_action"
    APPROVE_PLAN = "approve_plan"
    TAKE_OVER = "take_over"


DEFAULT_TRIGGER_LEVELS: dict[Trigger, ApprovalLevel] = {
    Trigger.LOW_PLAN_CONFIDENCE: ApprovalLevel.APPROVE_PLAN,
    Trigger.USER_REQUESTED: ApprovalLevel.APPROVE_PLAN,
    Trigger.SENSITIVE_OPERATION: ApprovalLevel.APPROVE_ACTION,
    Trigger.SPECIALIST_DOUBLE_FAILURE: ApprovalLevel.APPROVE_ACTION,  # take-over offered
    Trigger.LOW_REVIEW_SCORE: ApprovalLevel.APPROVE_ACTION,
}


def level_for(trigger: Trigger, overrides: dict[str, str] | None = None) -> ApprovalLevel:
    overrides = overrides if overrides is not None else get_settings().approval_level_overrides
    if trigger.value in overrides:
        return ApprovalLevel(overrides[trigger.value])
    return DEFAULT_TRIGGER_LEVELS[trigger]
