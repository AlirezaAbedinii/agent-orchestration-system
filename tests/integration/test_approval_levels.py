"""Resolution semantics per approval level: reject, modify, take-over,
sensitive-operation approve/deny, and NOTIFY (never pauses)."""

import pytest
import sqlalchemy as sa

from orchestrator.config import get_settings
from orchestrator.db.session import get_engine

VECTOR_REQUEST = (
    "Compare open-source vector databases: gather facts about Chroma from the web, "
    "compute the GitHub star ranking from the demo database, generate a comparison "
    "table using Python, and write a comparison memo saved as memo.md."
)
DOOMED_REQUEST = "TRIGGER-FAILURE: research the cursed topic thoroughly."
SENSITIVE_REQUEST = "TRIGGER-SENSITIVE: post the release announcement through the API."


def _pending_for(client, task_id):
    rows = client.get("/approvals", params={"status": "pending"}).json()["approvals"]
    (row,) = [r for r in rows if r["task_id"] == task_id]
    return row


def _api_call_invocations(task_id: str) -> list[str]:
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


def test_reject_at_plan_gate_terminates_with_reason(client):
    task_id = client.post(
        "/tasks", json={"request": VECTOR_REQUEST, "require_human_review": True}
    ).json()["task_id"]
    approval = _pending_for(client, task_id)

    client.post(
        f"/approvals/{approval['id']}/resolve",
        json={"action": "reject", "notes": "wrong direction, do not start"},
    )

    bundle = client.get(f"/tasks/{task_id}").json()
    assert bundle["status"] == "rejected"
    assert "wrong direction" in bundle["error"]
    assert all(s["status"] == "pending" for s in bundle["subtasks"])  # no work happened


def test_modify_at_plan_gate_executes_the_edited_plan(client):
    task_id = client.post(
        "/tasks", json={"request": VECTOR_REQUEST, "require_human_review": True}
    ).json()["task_id"]
    approval = _pending_for(client, task_id)

    # human trims the plan: drop the code subtask (s3)
    plan = approval["context"]["plan"]
    plan["subtasks"] = [s for s in plan["subtasks"] if s["id"] != "s3"]
    for subtask in plan["subtasks"]:
        subtask["depends_on"] = [d for d in subtask["depends_on"] if d != "s3"]

    resolved = client.post(
        f"/approvals/{approval['id']}/resolve",
        json={"action": "modify", "payload": {"plan": plan}, "notes": "code step unnecessary"},
    )
    assert resolved.status_code == 200

    # resumed with the edited plan; pauses again at the final gate
    final_approval = _pending_for(client, task_id)
    assert final_approval["gate_key"] == "final"
    client.post(f"/approvals/{final_approval['id']}/resolve", json={"action": "approve"})

    bundle = client.get(f"/tasks/{task_id}").json()
    assert bundle["status"] == "completed"
    assert {s["sid"] for s in bundle["subtasks"]} == {"s1", "s2", "s4"}
    assert all(s["status"] == "completed" for s in bundle["subtasks"])


def test_invalid_modified_plan_is_rejected_and_approval_stays_pending(client):
    task_id = client.post(
        "/tasks", json={"request": VECTOR_REQUEST, "require_human_review": True}
    ).json()["task_id"]
    approval = _pending_for(client, task_id)

    response = client.post(
        f"/approvals/{approval['id']}/resolve",
        json={"action": "modify", "payload": {"plan": {"subtasks": [], "confidence": 2}}},
    )
    assert response.status_code == 422
    assert client.get(f"/approvals/{approval['id']}").json()["status"] == "pending"


def test_double_failure_take_over_uses_human_output(client):
    task_id = client.post("/tasks", json={"request": DOOMED_REQUEST}).json()["task_id"]

    approval = _pending_for(client, task_id)
    assert approval["trigger"] == "specialist_double_failure"
    assert approval["level"] == "approve_action"
    assert approval["gate_key"].startswith("subtask:s1")
    assert approval["context"]["subtask_states"]["s1"]["error_count"] == 2

    client.post(
        f"/approvals/{approval['id']}/resolve",
        json={
            "action": "take_over",
            "payload": {"output": "HUMAN-RESEARCH: the cursed topic is fine, source: me"},
            "notes": "did it manually",
        },
    )

    bundle = client.get(f"/tasks/{task_id}").json()
    assert bundle["status"] == "completed"
    (subtask,) = bundle["subtasks"]
    assert subtask["status"] == "completed"
    assert subtask["output"].startswith("HUMAN-RESEARCH")
    assert bundle["final_output"].startswith("FINAL")


def test_sensitive_tool_call_pauses_then_runs_after_approval(client):
    task_id = client.post("/tasks", json={"request": SENSITIVE_REQUEST}).json()["task_id"]

    approval = _pending_for(client, task_id)
    assert approval["trigger"] == "sensitive_operation"
    assert approval["level"] == "approve_action"
    assert approval["gate_key"].startswith("tool:s1:api_call")
    assert approval["proposed_action"]["tool"] == "api_call"
    assert approval["proposed_action"]["arguments"]["method"] == "POST"
    assert _api_call_invocations(task_id) == []  # nothing ran while pending

    client.post(f"/approvals/{approval['id']}/resolve", json={"action": "approve"})

    bundle = client.get(f"/tasks/{task_id}").json()
    assert bundle["status"] == "completed"
    assert bundle["subtasks"][0]["output"].startswith("POSTED-OK")
    assert _api_call_invocations(task_id) == ["success"]


def test_sensitive_tool_call_reject_skips_the_call(client):
    task_id = client.post("/tasks", json={"request": SENSITIVE_REQUEST}).json()["task_id"]
    approval = _pending_for(client, task_id)

    client.post(
        f"/approvals/{approval['id']}/resolve",
        json={"action": "reject", "notes": "not cleared for external comms"},
    )

    bundle = client.get(f"/tasks/{task_id}").json()
    assert bundle["status"] == "completed"  # task finishes without the sensitive call
    assert bundle["subtasks"][0]["output"].startswith("SKIPPED-POST")
    assert _api_call_invocations(task_id) == []


@pytest.fixture()
def _notify_sensitive_ops():
    overrides = get_settings().approval_level_overrides
    overrides["sensitive_operation"] = "notify"
    yield
    overrides.pop("sensitive_operation", None)


def test_notify_level_informs_but_never_pauses(client, _notify_sensitive_ops):
    task_id = client.post("/tasks", json={"request": SENSITIVE_REQUEST}).json()["task_id"]

    # completed in one pass: no pending approvals, the call ran, one notified row
    bundle = client.get(f"/tasks/{task_id}").json()
    assert bundle["status"] == "completed"
    assert bundle["subtasks"][0]["output"].startswith("POSTED-OK")
    assert _api_call_invocations(task_id) == ["success"]

    rows = client.get("/approvals", params={"task_id": task_id}).json()["approvals"]
    (row,) = rows
    assert row["status"] == "notified"
    assert row["level"] == "notify"
    assert row["trigger"] == "sensitive_operation"
