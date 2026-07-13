"""Trace tree completeness, parent/child integrity, escalation spans, and the
cost endpoints (per-task hand-computed total + the four aggregate rollups)."""

import pytest

from orchestrator.llm.pricing import cost_usd

REQUEST = (
    "Compare open-source vector databases: gather facts about Chroma from the web, "
    "compute the GitHub star ranking from the demo database, generate a comparison "
    "table using Python, and write a comparison memo saved as memo.md."
)
SENSITIVE_REQUEST = "TRIGGER-SENSITIVE: post the release announcement through the API."


def _run_to_completion(client, request: str) -> str:
    task_id = client.post("/tasks", json={"request": request}).json()["task_id"]
    assert client.get(f"/tasks/{task_id}").json()["status"] == "completed"
    return task_id


def test_trace_tree_is_complete_with_intact_links(client):
    task_id = _run_to_completion(client, REQUEST)
    trace = client.get(f"/traces/{task_id}").json()
    spans = trace["spans"]
    by_id = {span["id"]: span for span in spans}

    # one root per run: this task ran once
    roots = [span for span in spans if span["parent_id"] is None]
    assert len(roots) == 1 and roots[0]["kind"] == "task"

    # every other span links to a parent within the same task's tree
    assert all(span["parent_id"] in by_id for span in spans if span["parent_id"])

    # all required span kinds are present
    kinds = {span["kind"] for span in spans}
    assert {"task", "planning", "specialist", "review", "tool", "memory", "synthesis", "llm"} <= kinds

    # planning hangs off the root; memory retrieval hangs off planning
    plan_span = next(span for span in spans if span["kind"] == "planning")
    assert plan_span["parent_id"] == roots[0]["id"]
    retrieve_span = next(span for span in spans if span["name"] == "memory:retrieve")
    assert retrieve_span["parent_id"] == plan_span["id"]
    assert retrieve_span["attributes"]["retrieved_count"] == 0  # clean store, first task

    # one specialist span per subtask; tools and reviews nest under their specialist
    specialist_spans = {span["attributes"]["sid"]: span for span in spans if span["kind"] == "specialist"}
    assert set(specialist_spans) == {"s1", "s2", "s3", "s4"}
    for tool_span in (span for span in spans if span["kind"] == "tool"):
        parent = by_id[tool_span["parent_id"]]
        assert parent["kind"] == "specialist"
        assert tool_span["attributes"]["sid"] == parent["attributes"]["sid"]
    review_spans = [span for span in spans if span["kind"] == "review"]
    assert len(review_spans) == 4
    assert all(by_id[span["parent_id"]]["kind"] == "specialist" for span in review_spans)
    assert all(span["attributes"]["score"] == 5 for span in review_spans)

    # memory extraction span recorded what it stored
    extract_span = next(span for span in spans if span["name"] == "memory:extract")
    assert extract_span["attributes"]["stored_count"] >= 3

    # everything succeeded
    assert all(span["status"] == "success" for span in spans)

    # llm calls carry the full prompt/response and link to llm spans in the tree
    calls = trace["llm_calls"]
    plan_call = next(
        call for call in calls
        if call["agent"] == "supervisor" and "Create an execution plan" in call["prompt"]
    )
    assert '"subtasks"' in plan_call["response"]
    llm_span = by_id[plan_call["span_id"]]
    assert llm_span["kind"] == "llm"
    assert llm_span["parent_id"] == plan_span["id"]


def test_escalation_spans_carry_trigger_and_resolution(client):
    task_id = client.post("/tasks", json={"request": SENSITIVE_REQUEST}).json()["task_id"]
    approval = [
        a for a in client.get("/approvals", params={"status": "pending"}).json()["approvals"]
        if a["task_id"] == task_id
    ][0]
    client.post(f"/approvals/{approval['id']}/resolve", json={"action": "approve"})
    assert client.get(f"/tasks/{task_id}").json()["status"] == "completed"

    spans = client.get(f"/traces/{task_id}").json()["spans"]
    roots = [span for span in spans if span["parent_id"] is None]
    assert {root["name"] for root in roots} == {"task", "task:resume"}  # pause + resume runs

    escalations = [span for span in spans if span["kind"] == "escalation"]
    assert all(span["attributes"]["trigger"] == "sensitive_operation" for span in escalations)
    assert any(span["status"] == "escalated" for span in escalations)  # the pause
    assert any(span["attributes"].get("resolution") == "approve" for span in escalations)  # the resume


def test_cost_endpoint_matches_hand_computed_fixture_costs(client):
    task_id = _run_to_completion(client, REQUEST)
    costs = client.get(f"/traces/{task_id}/costs").json()

    # hand-computed from the fixtures' declared token counts:
    #   supervisor: plan (450/280) + synthesize (300/40) on gpt-4o
    #   reviewer: 4 evaluations at 120/15 on claude-sonnet-5
    #   memory: 1 extraction at 250/90 on gpt-4o-mini
    #   specialists: fixtures declare no usage -> 0
    expected = (
        cost_usd("gpt-4o", 450, 280)
        + cost_usd("gpt-4o", 300, 40)
        + 4 * cost_usd("claude-sonnet-5", 120, 15)
        + cost_usd("gpt-4o-mini", 250, 90)
    )
    assert costs["total_usd"] == pytest.approx(expected)

    by_agent = {row["agent"]: row for row in costs["llm"]["by_agent_model"]}
    assert by_agent["supervisor"]["prompt_tokens"] == 750
    assert by_agent["supervisor"]["completion_tokens"] == 320
    assert by_agent["reviewer"]["calls"] == 4
    assert costs["total_tool_calls"] >= 4
    assert costs["wall_clock_s"] > 0
    assert costs["human_review_seconds"] == 0.0
    assert costs["escalations"] == 0


def test_aggregate_rollups(client):
    _run_to_completion(client, REQUEST)
    aggregates = client.get("/traces/aggregates/costs").json()

    types = {row["task_type"] for row in aggregates["cost_by_task_type"]}
    assert "analysis+code+research+writing" in types

    assert aggregates["most_expensive_agents"][0]["agent"] == "supervisor"

    assert aggregates["tool_usage"]["web_search"]["success"] == 1
    assert aggregates["tool_usage"]["file_write"]["total"] >= 1

    (day,) = aggregates["escalation_trend"]
    assert day["tasks"] == 1 and day["escalated"] == 0 and day["rate"] == 0.0
