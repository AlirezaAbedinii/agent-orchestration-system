"""Approval queue semantics (in-memory drop-in) and webhook notification."""

import time

import pytest

import orchestrator.hitl.notify as notify_module
from orchestrator.hitl.notify import notify_approval
from orchestrator.hitl.queue import InMemoryApprovalQueue


def _ensure(queue, gate_key="plan", task_id="t1"):
    return queue.ensure(
        task_id=task_id,
        gate_key=gate_key,
        trigger="low_plan_confidence",
        level="approve_plan",
        context={"request": "do a thing"},
        proposed_action={"type": "execute_plan"},
        reasoning="confidence 0.3 below 0.7",
    )


def test_ensure_is_idempotent_and_notifies_once():
    notifications = []
    queue = InMemoryApprovalQueue(notifier=notifications.append)

    first_id, created = _ensure(queue)
    assert created is True
    again_id, created_again = _ensure(queue)  # node re-execution after resume
    assert again_id == first_id
    assert created_again is False
    assert len(notifications) == 1
    assert notifications[0]["task_id"] == "t1"


def test_resolve_records_action_payload_notes_and_review_time():
    queue = InMemoryApprovalQueue()
    approval_id, _ = _ensure(queue)
    time.sleep(0.01)

    resolved = queue.resolve(
        approval_id, action="modify", payload={"plan": {"subtasks": []}}, notes="trimmed scope"
    )

    assert resolved["status"] == "resolved"
    assert resolved["resolution_action"] == "modify"
    assert resolved["resolution_payload"] == {"plan": {"subtasks": []}}
    assert resolved["resolution_notes"] == "trimmed scope"
    assert resolved["review_seconds"] > 0  # human review time recorded


def test_resolving_twice_is_rejected():
    queue = InMemoryApprovalQueue()
    approval_id, _ = _ensure(queue)
    queue.resolve(approval_id, action="approve")
    with pytest.raises(ValueError, match="not pending"):
        queue.resolve(approval_id, action="reject")


def test_record_notify_never_creates_pending_work():
    queue = InMemoryApprovalQueue()
    queue.record_notify(
        task_id="t1", gate_key="tool:s1:api_call:0", trigger="sensitive_operation",
        context={}, proposed_action={"tool": "api_call"}, reasoning="POST call",
    )
    assert queue.list(status="pending") == []
    (row,) = queue.list(status="notified")
    assert row["level"] == "notify"


def test_list_filters_by_status_and_task():
    queue = InMemoryApprovalQueue()
    first, _ = _ensure(queue, gate_key="plan", task_id="t1")
    _ensure(queue, gate_key="final", task_id="t2")
    queue.resolve(first, action="approve")
    assert {r["task_id"] for r in queue.list(status="pending")} == {"t2"}
    assert len(queue.list(task_id="t1")) == 1


def test_webhook_notification_posts_slack_payload(monkeypatch):
    posted = {}

    def fake_post(url, json=None, timeout=None):
        posted["url"], posted["json"] = url, json

        class Response:
            def raise_for_status(self):
                return None

        return Response()

    monkeypatch.setattr(notify_module.httpx, "post", fake_post)
    notify_approval(
        {"id": "a1", "task_id": "t1", "trigger": "sensitive_operation",
         "level": "approve_action", "reasoning": "POST about to run"},
        webhook_url="https://hooks.example.com/T000/B000",
    )
    assert posted["url"] == "https://hooks.example.com/T000/B000"
    assert "Approval needed" in posted["json"]["text"]
    assert "t1" in posted["json"]["text"]


def test_webhook_failure_never_raises(monkeypatch):
    def broken_post(url, json=None, timeout=None):
        raise ConnectionError("webhook down")

    monkeypatch.setattr(notify_module.httpx, "post", broken_post)
    notify_approval({"id": "a1", "task_id": "t1"}, webhook_url="https://hooks.example.com/x")
