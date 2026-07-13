"""Full-lifecycle test (plan §7): the §5.1 demo scenario start-to-finish with
programmatic approvals.

Asserts the final output, memory write-back, cleared working memory, a
complete trace tree (including the parallel research fan-out, the reviewer
rejection → rework, and both escalation gates), and a non-zero computed cost.
"""

from orchestrator.config import get_settings

WARMUP_REQUEST = (
    "Compare open-source vector databases: gather facts about Chroma from the web, "
    "compute the GitHub star ranking from the demo database, generate a comparison "
    "table using Python, and write a comparison memo saved as memo.md."
)
DEMO_REQUEST = (
    "Research the top 3 open-source vector databases (Chroma, Qdrant, Weaviate), "
    "extract and compare their GitHub statistics, analyze the trade-offs, and "
    "produce a one-page recommendation memo with cited sources."
)

REQUIRED_SPAN_KINDS = {
    "task", "planning", "specialist", "review", "tool",
    "memory", "synthesis", "escalation", "llm",
}


def _pending_one(client, task_id):
    rows = client.get("/approvals", params={"status": "pending"}).json()["approvals"]
    (row,) = [r for r in rows if r["task_id"] == task_id]
    return row


def test_full_lifecycle_demo_scenario(client):
    # a prior run seeds long-term memory so the showcase planning is informed
    warmup = client.post("/tasks", json={"request": WARMUP_REQUEST}).json()["task_id"]
    assert client.get(f"/tasks/{warmup}").json()["status"] == "completed"

    task_id = client.post(
        "/tasks", json={"request": DEMO_REQUEST, "require_human_review": True}
    ).json()["task_id"]

    # gate 1: the plan
    approval = _pending_one(client, task_id)
    assert (approval["gate_key"], approval["trigger"]) == ("plan", "user_requested")
    client.post(f"/approvals/{approval['id']}/resolve", json={"action": "approve"})

    # gate 2: the final deliverable (all agent work already reviewed)
    final = _pending_one(client, task_id)
    assert final["gate_key"] == "final"
    assert final["proposed_action"]["final_output"].startswith("FINAL RECOMMENDATION")
    client.post(f"/approvals/{final['id']}/resolve", json={"action": "approve"})

    # --- final output -------------------------------------------------------
    bundle = client.get(f"/tasks/{task_id}").json()
    assert bundle["status"] == "completed"
    assert bundle["final_output"].startswith("FINAL RECOMMENDATION")
    assert {s["sid"]: s["status"] for s in bundle["subtasks"]} == {
        sid: "completed" for sid in ("a1", "r1", "r2", "r3", "w1")
    }

    # the reworked memo (with citations) landed in the task workspace
    memo = get_settings().workspace_root / task_id / "recommendation.md"
    assert memo.is_file()
    assert "https://github.com/qdrant/qdrant" in memo.read_text(encoding="utf-8")

    # parallel research fan-out, analysis fan-in, then the rework wave
    from orchestrator.graph.runner import get_production_graph

    state = get_production_graph().get_state({"configurable": {"thread_id": task_id}})
    assert state.values["dispatch_log"] == [["r1", "r2", "r3"], ["a1"], ["w1"], ["w1"]]

    # --- trace tree ---------------------------------------------------------
    trace = client.get(f"/traces/{task_id}").json()
    spans = trace["spans"]
    span_ids = {span["id"] for span in spans}
    assert all(span["parent_id"] in span_ids for span in spans if span["parent_id"])
    assert REQUIRED_SPAN_KINDS <= {span["kind"] for span in spans}

    retrieve = next(span for span in spans if span["name"] == "memory:retrieve")
    assert retrieve["attributes"]["retrieved_count"] >= 1  # memory-informed planning

    w1_reviews = sorted(
        (s for s in spans if s["kind"] == "review" and s["attributes"]["sid"] == "w1"),
        key=lambda span: span["start_time"],
    )
    assert [span["attributes"]["score"] for span in w1_reviews] == [2, 5]  # reject → rework

    # --- cost ---------------------------------------------------------------
    costs = client.get(f"/traces/{task_id}/costs").json()
    assert costs["total_usd"] > 0
    assert costs["llm"]["total_prompt_tokens"] > 0
    assert costs["tool_calls"]["web_search"]["success"] == 3
    assert costs["tool_calls"]["db_query"]["success"] == 1
    assert costs["tool_calls"]["code_exec"]["success"] == 1
    assert costs["tool_calls"]["file_write"]["success"] == 1
    assert costs["escalations"] == 2
    assert costs["human_review_seconds"] >= 0

    # --- memory write-back & working-memory cleanup --------------------------
    extract = next(span for span in spans if span["name"] == "memory:extract")
    assert extract["attributes"]["stored_count"] >= 1

    dashboard = client.get("/memory/users/default").json()
    assert sum(dashboard["counts"].values()) >= 4  # warm-up + demo extractions

    from orchestrator.memory.working import WorkingMemory

    assert not WorkingMemory().exists(task_id)
