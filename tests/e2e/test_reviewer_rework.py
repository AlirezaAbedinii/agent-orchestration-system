"""E2E 3 (plan §7): the reviewer catches deliberately bad output.

The writing specialist's first memo draft is fixture-corrupted — it cites
nothing ("trust me on the numbers"). The reviewer must reject it with
actionable feedback, the rework loop must re-dispatch the specialist with that
feedback, and the second attempt must pass.
"""

from orchestrator.agents.specialists.base import FEEDBACK_MARKER

DEMO_REQUEST = (
    "Research the top 3 open-source vector databases (Chroma, Qdrant, Weaviate), "
    "extract and compare their GitHub statistics, analyze the trade-offs, and "
    "produce a one-page recommendation memo with cited sources."
)


def test_reviewer_rejects_citation_free_draft_then_rework_passes(client):
    task_id = client.post("/tasks", json={"request": DEMO_REQUEST}).json()["task_id"]
    bundle = client.get(f"/tasks/{task_id}").json()
    assert bundle["status"] == "completed"

    w1 = next(s for s in bundle["subtasks"] if s["sid"] == "w1")
    assert w1["status"] == "completed"
    assert w1["review_score"] >= 3

    trace = client.get(f"/traces/{task_id}").json()

    # the reviewer scored the draft 2/5 with concrete feedback, then 5/5
    w1_reviews = sorted(
        (s for s in trace["spans"] if s["kind"] == "review" and s["attributes"]["sid"] == "w1"),
        key=lambda span: span["start_time"],
    )
    assert [span["attributes"]["score"] for span in w1_reviews] == [2, 5]
    assert "Missing citations" in w1_reviews[0]["attributes"]["feedback"]

    # the rework prompt carried the reviewer's feedback back to the specialist
    rework_prompts = [
        call["prompt"]
        for call in trace["llm_calls"]
        if call["agent"] == "writing" and FEEDBACK_MARKER in call["prompt"]
    ]
    assert rework_prompts and any("Missing citations" in p for p in rework_prompts)

    # w1 was dispatched twice: the draft wave and the rework wave
    from orchestrator.graph.runner import get_production_graph

    state = get_production_graph().get_state({"configurable": {"thread_id": task_id}})
    assert state.values["dispatch_log"].count(["w1"]) == 2
