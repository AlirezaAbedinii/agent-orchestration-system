"""E2E 5 (plan §7): escalation triggers at the right moments.

Each of the five triggers fires under a simulated condition, maps to its
configured approval level, pauses the run, and resumes when the human
approves.
"""

import sqlalchemy as sa

from orchestrator.db.session import get_engine

VECTOR_REQUEST = (
    "Compare open-source vector databases: gather facts about Chroma from the web, "
    "compute the GitHub star ranking from the demo database, generate a comparison "
    "table using Python, and write a comparison memo saved as memo.md."
)
LOWCONF_REQUEST = "TRIGGER-LOWCONF: survey the shaky emerging topic before we invest."
LOWSCORE_REQUEST = "TRIGGER-LOWSCORE: draft the product blurb for the launch."
DOOMED_REQUEST = "TRIGGER-FAILURE: research the cursed topic thoroughly."
SENSITIVE_REQUEST = "TRIGGER-SENSITIVE: post the release announcement through the API."


def _pending_one(client, task_id):
    rows = client.get("/approvals", params={"status": "pending"}).json()["approvals"]
    (row,) = [r for r in rows if r["task_id"] == task_id]
    return row


def _approve(client, approval, notes=""):
    response = client.post(
        f"/approvals/{approval['id']}/resolve", json={"action": "approve", "notes": notes}
    )
    assert response.status_code == 200


def test_user_requested_review_pauses_at_plan_and_final_gates(client):
    task_id = client.post(
        "/tasks", json={"request": VECTOR_REQUEST, "require_human_review": True}
    ).json()["task_id"]

    approval = _pending_one(client, task_id)
    assert (approval["trigger"], approval["level"]) == ("user_requested", "approve_plan")
    bundle = client.get(f"/tasks/{task_id}").json()
    assert bundle["status"] == "awaiting_approval"
    assert all(s["status"] == "pending" for s in bundle["subtasks"])  # paused before any work

    _approve(client, approval)
    final = _pending_one(client, task_id)
    assert (final["trigger"], final["level"]) == ("user_requested", "approve_action")
    assert final["gate_key"] == "final"
    _approve(client, final)
    assert client.get(f"/tasks/{task_id}").json()["status"] == "completed"


def test_low_plan_confidence_pauses_then_resumes_on_approve(client):
    task_id = client.post("/tasks", json={"request": LOWCONF_REQUEST}).json()["task_id"]

    approval = _pending_one(client, task_id)
    assert (approval["trigger"], approval["level"]) == ("low_plan_confidence", "approve_plan")
    assert "below the threshold" in approval["reasoning"]
    assert client.get(f"/tasks/{task_id}").json()["status"] == "awaiting_approval"

    _approve(client, approval)
    bundle = client.get(f"/tasks/{task_id}").json()
    assert bundle["status"] == "completed"
    assert bundle["subtasks"][0]["output"].startswith("SHAKY-NOTES")


def test_specialist_double_failure_pauses_and_approve_retries(client):
    task_id = client.post("/tasks", json={"request": DOOMED_REQUEST}).json()["task_id"]

    approval = _pending_one(client, task_id)
    assert (approval["trigger"], approval["level"]) == (
        "specialist_double_failure", "approve_action",
    )
    assert approval["context"]["subtask_states"]["s1"]["error_count"] == 2

    # approve = retry; the specialist is still doomed, so it escalates again
    _approve(client, approval)
    second = _pending_one(client, task_id)
    assert second["id"] != approval["id"]
    assert second["trigger"] == "specialist_double_failure"

    # the human takes over to finish cleanly
    response = client.post(
        f"/approvals/{second['id']}/resolve",
        json={"action": "take_over", "payload": {"output": "HUMAN-RESEARCH: handled manually"}},
    )
    assert response.status_code == 200
    assert client.get(f"/tasks/{task_id}").json()["status"] == "completed"


def test_sensitive_operation_pauses_before_the_call_runs(client):
    task_id = client.post("/tasks", json={"request": SENSITIVE_REQUEST}).json()["task_id"]

    approval = _pending_one(client, task_id)
    assert (approval["trigger"], approval["level"]) == ("sensitive_operation", "approve_action")
    assert approval["proposed_action"]["tool"] == "api_call"

    def api_calls() -> list[str]:
        with get_engine().connect() as connection:
            return [
                status
                for (status,) in connection.execute(
                    sa.text(
                        "SELECT status FROM tool_invocations "
                        "WHERE task_id = :t AND tool_name = 'api_call'"
                    ),
                    {"t": task_id},
                )
            ]

    assert api_calls() == []  # nothing ran while paused
    _approve(client, approval)
    bundle = client.get(f"/tasks/{task_id}").json()
    assert bundle["status"] == "completed"
    assert bundle["subtasks"][0]["output"].startswith("POSTED-OK")
    assert api_calls() == ["success"]


def test_low_review_score_after_rework_pauses_then_resumes_on_approve(client):
    task_id = client.post("/tasks", json={"request": LOWSCORE_REQUEST}).json()["task_id"]

    approval = _pending_one(client, task_id)
    assert (approval["trigger"], approval["level"]) == ("low_review_score", "approve_action")
    assert "stayed below" in approval["reasoning"]
    assert approval["context"]["subtask_states"]["s1"]["rework_count"] == 2

    # approve with guidance; the retry uses it and passes review
    _approve(client, approval, notes="ship it")
    bundle = client.get(f"/tasks/{task_id}").json()
    assert bundle["status"] == "completed"
    assert bundle["subtasks"][0]["output"].startswith("STRONG-BLURB")
    assert bundle["subtasks"][0]["review_score"] == 5
