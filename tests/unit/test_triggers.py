"""All five escalation triggers as pure predicates + trigger→level mapping."""

import pytest

from orchestrator.config import Settings
from orchestrator.hitl.levels import ApprovalLevel, level_for
from orchestrator.hitl.triggers import (
    Trigger,
    low_plan_confidence,
    low_review_score,
    plan_escalation,
    sensitive_operation,
    specialist_double_failure,
    subtask_escalation,
    user_requested,
)


@pytest.mark.parametrize(
    ("confidence", "threshold", "fires"),
    [(0.3, 0.7, True), (0.69, 0.7, True), (0.7, 0.7, False), (0.95, 0.7, False)],
)
def test_low_plan_confidence(confidence, threshold, fires):
    escalation = low_plan_confidence(confidence, threshold)
    assert (escalation is not None) is fires
    if fires:
        assert escalation.trigger is Trigger.LOW_PLAN_CONFIDENCE


@pytest.mark.parametrize(("flag", "fires"), [(True, True), (False, False)])
def test_user_requested(flag, fires):
    assert (user_requested(flag) is not None) is fires


@pytest.mark.parametrize(("errors", "fires"), [(0, False), (1, False), (2, True), (3, True)])
def test_specialist_double_failure(errors, fires):
    escalation = specialist_double_failure("s1", errors, last_error="boom")
    assert (escalation is not None) is fires
    if fires:
        assert "s1" in escalation.reason and "boom" in escalation.reason


@pytest.mark.parametrize(("sensitive", "fires"), [(True, True), (False, False)])
def test_sensitive_operation(sensitive, fires):
    assert (sensitive_operation("api_call", sensitive) is not None) is fires


@pytest.mark.parametrize(
    ("score", "rework", "fires"),
    [(2, 2, True), (2, 1, False), (4, 2, False), (None, 2, False)],
)
def test_low_review_score(score, rework, fires):
    escalation = low_review_score("s1", score, rework, threshold=3, max_retries=2)
    assert (escalation is not None) is fires


def test_plan_escalation_precedence():
    both = plan_escalation(0.2, True, 0.7)
    assert both.trigger is Trigger.LOW_PLAN_CONFIDENCE  # more informative label wins
    assert plan_escalation(0.9, True, 0.7).trigger is Trigger.USER_REQUESTED
    assert plan_escalation(0.9, False, 0.7) is None


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"status": "failed_attempt", "error_count": 2, "error": "x"}, Trigger.SPECIALIST_DOUBLE_FAILURE),
        ({"status": "failed_attempt", "error_count": 1}, None),
        ({"status": "rework", "score": 2, "rework_count": 2}, Trigger.LOW_REVIEW_SCORE),
        ({"status": "rework", "score": 2, "rework_count": 1}, None),
        ({"status": "completed"}, None),
    ],
)
def test_subtask_escalation(result, expected):
    escalation = subtask_escalation("s1", result, review_threshold=3, max_retries=2)
    assert (escalation.trigger if escalation else None) == expected


@pytest.mark.parametrize(
    ("trigger", "level"),
    [
        (Trigger.LOW_PLAN_CONFIDENCE, ApprovalLevel.APPROVE_PLAN),
        (Trigger.USER_REQUESTED, ApprovalLevel.APPROVE_PLAN),
        (Trigger.SENSITIVE_OPERATION, ApprovalLevel.APPROVE_ACTION),
        (Trigger.SPECIALIST_DOUBLE_FAILURE, ApprovalLevel.APPROVE_ACTION),
        (Trigger.LOW_REVIEW_SCORE, ApprovalLevel.APPROVE_ACTION),
    ],
)
def test_default_trigger_level_mapping(trigger, level):
    assert level_for(trigger, overrides={}) is level


def test_level_overrides_win():
    overrides = {"sensitive_operation": "notify", "low_review_score": "take_over"}
    assert level_for(Trigger.SENSITIVE_OPERATION, overrides) is ApprovalLevel.NOTIFY
    assert level_for(Trigger.LOW_REVIEW_SCORE, overrides) is ApprovalLevel.TAKE_OVER
    assert level_for(Trigger.USER_REQUESTED, overrides) is ApprovalLevel.APPROVE_PLAN


def test_overrides_parse_from_environment(monkeypatch):
    monkeypatch.setenv("APPROVAL_LEVEL_OVERRIDES", '{"specialist_double_failure": "take_over"}')
    settings = Settings(_env_file=None)
    assert level_for(Trigger.SPECIALIST_DOUBLE_FAILURE, settings.approval_level_overrides) is (
        ApprovalLevel.TAKE_OVER
    )
