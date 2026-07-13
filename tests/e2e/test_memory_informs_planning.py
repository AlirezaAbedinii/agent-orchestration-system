"""E2E 4 (plan §7): memory improves planning on repeated similar tasks.

After a completed comparison task, a similar research task must retrieve at
least one extracted memory, the planning prompt must literally contain it, and
the retrieval must be recorded in the execution trace.
"""

from orchestrator.memory.retrieval import MEMORIES_MARKER
from orchestrator.planning.decomposer import PLAN_MARKER

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


def _planning_prompt(trace: dict) -> str:
    return next(
        call["prompt"]
        for call in trace["llm_calls"]
        if call["agent"] == "supervisor" and PLAN_MARKER in call["prompt"]
    )


def test_second_similar_task_plans_with_retrieved_memory(client):
    first = client.post("/tasks", json={"request": VECTOR_REQUEST}).json()["task_id"]
    assert client.get(f"/tasks/{first}").json()["status"] == "completed"

    second = client.post("/tasks", json={"request": DEMO_REQUEST}).json()["task_id"]
    assert client.get(f"/tasks/{second}").json()["status"] == "completed"

    # the first task planned against an empty store — no memories block
    assert MEMORIES_MARKER not in _planning_prompt(client.get(f"/traces/{first}").json())

    trace = client.get(f"/traces/{second}").json()

    # the planning prompt contains a memory extracted from the first task
    prompt = _planning_prompt(trace)
    assert MEMORIES_MARKER in prompt
    assert "Chroma is an open-source embedding database" in prompt

    # the retrieval is recorded in the trace, with the retrieved ids
    retrieve = next(span for span in trace["spans"] if span["name"] == "memory:retrieve")
    assert retrieve["attributes"]["retrieved_count"] >= 1
    assert retrieve["attributes"]["memory_ids"]
