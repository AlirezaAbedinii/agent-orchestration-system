import pytest
from pydantic import ValidationError

from orchestrator.planning.schemas import ExecutionPlan, Subtask


def _subtask(sid: str, specialist: str = "research", depends_on: list[str] | None = None) -> dict:
    return {
        "id": sid,
        "description": f"do {sid}",
        "specialist": specialist,
        "depends_on": depends_on or [],
    }


def test_valid_plan_and_waves():
    plan = ExecutionPlan(
        subtasks=[
            _subtask("s1"),
            _subtask("s2", "analysis"),
            _subtask("s3", "writing", depends_on=["s1", "s2"]),
        ],
        confidence=0.8,
    )
    assert plan.topological_waves() == [["s1", "s2"], ["s3"]]
    assert plan.subtask("s3").specialist == "writing"


def test_duplicate_ids_rejected():
    with pytest.raises(ValidationError, match="Duplicate subtask ids"):
        ExecutionPlan(subtasks=[_subtask("s1"), _subtask("s1")], confidence=0.8)


def test_unknown_dependency_rejected():
    with pytest.raises(ValidationError, match="unknown ids"):
        ExecutionPlan(subtasks=[_subtask("s1", depends_on=["nope"])], confidence=0.8)


def test_self_dependency_rejected():
    with pytest.raises(ValidationError, match="depends on itself"):
        ExecutionPlan(subtasks=[_subtask("s1", depends_on=["s1"])], confidence=0.8)


def test_cycle_rejected():
    with pytest.raises(ValidationError, match="cycle"):
        ExecutionPlan(
            subtasks=[_subtask("s1", depends_on=["s2"]), _subtask("s2", depends_on=["s1"])],
            confidence=0.8,
        )


def test_confidence_bounds():
    with pytest.raises(ValidationError):
        ExecutionPlan(subtasks=[_subtask("s1")], confidence=1.2)
    with pytest.raises(ValidationError):
        ExecutionPlan(subtasks=[_subtask("s1")], confidence=-0.1)


def test_unknown_specialist_rejected():
    with pytest.raises(ValidationError):
        Subtask(id="s1", description="x", specialist="astrologer")


def test_at_least_one_subtask():
    with pytest.raises(ValidationError):
        ExecutionPlan(subtasks=[], confidence=0.5)
