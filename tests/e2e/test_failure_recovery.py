"""E2E 6 (plan §7): graceful recovery from agent failures.

A fixture forces the research specialist to call a nonexistent tool: the first
failure retries with a revised-approach instruction, the second failure
escalates, and at no point is the task left in an inconsistent state — it
pauses cleanly, keeps its working memory for the resume, and completes after a
human take-over.
"""

DOOMED_REQUEST = "TRIGGER-FAILURE: research the cursed topic thoroughly."


def test_forced_failures_retry_then_escalate_without_corrupting_state(client):
    task_id = client.post("/tasks", json={"request": DOOMED_REQUEST}).json()["task_id"]

    bundle = client.get(f"/tasks/{task_id}").json()
    assert bundle["status"] == "awaiting_approval"  # paused, not crashed
    (s1,) = bundle["subtasks"]
    assert s1["status"] == "failed"
    assert s1["attempts"] == 2
    assert s1["error"]
    assert bundle["final_output"] is None

    trace = client.get(f"/traces/{task_id}").json()

    # the retry carried a revised-approach instruction into the prompt
    research_prompts = [c["prompt"] for c in trace["llm_calls"] if c["agent"] == "research"]
    assert len(research_prompts) == 2
    assert "Try a different approach" in research_prompts[1]

    # both attempts traced as failures; the escalation span carries the trigger
    attempts = [s for s in trace["spans"] if s["kind"] == "specialist"]
    assert len(attempts) == 2
    assert all(span["status"] == "failure" for span in attempts)
    escalation = next(s for s in trace["spans"] if s["kind"] == "escalation")
    assert escalation["attributes"]["trigger"] == "specialist_double_failure"

    # working memory survives the pause — the resume needs it
    from orchestrator.memory.working import WorkingMemory

    assert WorkingMemory().exists(task_id)

    # human takes over → the task completes with the human output
    rows = client.get("/approvals", params={"status": "pending"}).json()["approvals"]
    (approval,) = [r for r in rows if r["task_id"] == task_id]
    response = client.post(
        f"/approvals/{approval['id']}/resolve",
        json={
            "action": "take_over",
            "payload": {"output": "HUMAN-RESEARCH: the cursed topic is fine, source: me"},
            "notes": "did it manually",
        },
    )
    assert response.status_code == 200

    bundle = client.get(f"/tasks/{task_id}").json()
    assert bundle["status"] == "completed"
    (s1,) = bundle["subtasks"]
    assert s1["status"] == "completed"
    assert s1["output"].startswith("HUMAN-RESEARCH")
    assert bundle["final_output"].startswith("FINAL")

    # working memory cleared on completion — nothing leaks
    assert not WorkingMemory().exists(task_id)
