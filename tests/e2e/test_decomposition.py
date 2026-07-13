"""E2E 1 (plan §7): task decomposition produces valid plans.

Three differently-shaped requests — a diamond (three parallel subtasks feeding
one), a two-stage fan-in pipeline, and a single-node plan. Every plan must be
schema-valid, acyclic, assigned to known specialists, and carry a confidence.
"""

import pytest

from orchestrator.llm.router import SPECIALISTS
from orchestrator.planning.schemas import ExecutionPlan

VECTOR_REQUEST = (
    "Compare open-source vector databases: gather facts about Chroma from the web, "
    "compute the GitHub star ranking from the demo database, generate a comparison "
    "table using Python, and write a comparison memo saved as memo.md."
)
DEMO_REQUEST = (
    "Research the top 3 open-source vector databases (Chroma, Qdrant, Weaviate), "
    "extract and compare their GitHub statistics, analyze the trade-offs, and "
    "produce a one-page recommendation memo with cited sources."
)
SENSITIVE_REQUEST = "TRIGGER-SENSITIVE: post the release announcement through the API."

CASES = [
    pytest.param(VECTOR_REQUEST, 4, {"s4": {"s1", "s2", "s3"}}, id="diamond"),
    pytest.param(
        DEMO_REQUEST,
        5,
        {"a1": {"r1", "r2", "r3"}, "w1": {"r1", "r2", "r3", "a1"}},
        id="fan-in-pipeline",
    ),
    pytest.param(SENSITIVE_REQUEST, 1, {}, id="single-node"),
]


@pytest.mark.parametrize("request_text,subtask_count,expected_deps", CASES)
def test_decomposition_produces_valid_plan(client, request_text, subtask_count, expected_deps):
    task_id = client.post("/tasks", json={"request": request_text}).json()["task_id"]
    raw_plan = client.get(f"/tasks/{task_id}").json()["plan"]
    assert raw_plan is not None

    # schema validation enforces unique ids, known specialists, an acyclic DAG
    plan = ExecutionPlan.model_validate(raw_plan)
    assert len(plan.subtasks) == subtask_count
    assert 0.0 <= plan.confidence <= 1.0
    assert {s.specialist for s in plan.subtasks} <= set(SPECIALISTS)

    dependencies = {s.id: set(s.depends_on) for s in plan.subtasks}
    for sid, expected in expected_deps.items():
        assert dependencies[sid] == expected
