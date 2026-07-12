"""Graph conditional-edge tests: run the full LangGraph with scripted mock
fixtures, an in-memory repo, and an in-memory invocation store (no Postgres)."""

import json

import pytest

from orchestrator.db.repo import InMemoryTaskRepo
from orchestrator.graph.builder import build_graph
from orchestrator.llm.mock import MockLLMClient
from orchestrator.tools.base import InMemoryInvocationStore
from orchestrator.tools.defaults import build_default_registry


def fx(directory, name: str, agent: str, text: str, match: list[str] | None = None) -> None:
    payload = {"agent": agent, "response": {"text": text}}
    if match:
        payload["match"] = match
    (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def plan_text(subtasks: list[dict], confidence: float) -> str:
    return json.dumps({"task_summary": "test", "subtasks": subtasks, "confidence": confidence})


def subtask(sid: str, specialist: str, description: str, depends_on: list[str] | None = None) -> dict:
    return {
        "id": sid,
        "description": description,
        "specialist": specialist,
        "required_inputs": [],
        "expected_output_format": "plain text",
        "estimated_complexity": "low",
        "depends_on": depends_on or [],
    }


def final(output: str) -> str:
    return json.dumps({"action": "final", "output": output})


def tool_call(tool: str, arguments: dict) -> str:
    return json.dumps({"action": "tool", "tool": tool, "arguments": arguments})


def verdict(score: int, feedback: str = "") -> str:
    return json.dumps({"score": score, "feedback": feedback})


@pytest.fixture()
def repo():
    return InMemoryTaskRepo()


def run(tmp_path, repo, request_text: str):
    graph = build_graph(
        llm=MockLLMClient(tmp_path),
        registry=build_default_registry(InMemoryInvocationStore()),
        repo=repo,
        checkpointer=None,
    )
    task_id = repo.create_task(request_text)
    return task_id, graph.invoke(
        {"task_id": task_id, "request": request_text, "subtask_results": {}, "dispatch_log": []}
    )


def test_happy_path_with_parallel_wave(tmp_path, repo):
    fx(tmp_path, "plan", "supervisor",
       plan_text([subtask("s1", "research", "look up A"),
                  subtask("s2", "research", "look up B"),
                  subtask("s3", "writing", "write summary", depends_on=["s1", "s2"])], 0.9),
       match=["Create an execution plan"])
    fx(tmp_path, "synth", "supervisor", "DONE", match=["Synthesize the final deliverable"])
    fx(tmp_path, "research", "research", final("RESEARCH-OK"))
    fx(tmp_path, "writing", "writing", final("SUMMARY-OK"))
    fx(tmp_path, "reviewer", "reviewer", verdict(5, "solid"))

    task_id, state = run(tmp_path, repo, "compare A and B")

    assert state["final_output"] == "DONE"
    assert state["dispatch_log"][0] == ["s1", "s2"]  # independent subtasks batched together
    assert state["dispatch_log"][1] == ["s3"]
    assert all(r["status"] == "completed" for r in state["subtask_results"].values())
    assert repo.tasks[task_id]["status"] == "completed"
    assert repo.tasks[task_id]["final_output"] == "DONE"


def test_reviewer_rejection_routes_back_with_feedback_then_succeeds(tmp_path, repo):
    fx(tmp_path, "plan", "supervisor",
       plan_text([subtask("s1", "writing", "draft the memo")], 0.9),
       match=["Create an execution plan"])
    fx(tmp_path, "synth", "supervisor", "DONE", match=["Synthesize the final deliverable"])
    # attempt 2 carries the reviewer feedback block; filename sorts first so it wins
    fx(tmp_path, "a_writing_feedback", "writing", final("DRAFT-V2"), match=["Reviewer feedback:"])
    fx(tmp_path, "b_writing_first", "writing", final("DRAFT-V1"), match=["Transcript: (none yet)"])
    fx(tmp_path, "r_v1", "reviewer", verdict(2, "add citations"), match=["DRAFT-V1"])
    fx(tmp_path, "r_v2", "reviewer", verdict(5, "good"), match=["DRAFT-V2"])

    task_id, state = run(tmp_path, repo, "write a memo")

    result = state["subtask_results"]["s1"]
    assert result["status"] == "completed"
    assert result["output"] == "DRAFT-V2"
    assert result["attempts"] == 2
    assert result["rework_count"] == 1
    assert repo.subtasks[task_id]["s1"]["review_score"] == 5
    assert repo.tasks[task_id]["status"] == "completed"


def test_two_specialist_failures_set_needs_escalation(tmp_path, repo):
    fx(tmp_path, "plan", "supervisor",
       plan_text([subtask("s1", "research", "look something up")], 0.9),
       match=["Create an execution plan"])
    # the specialist insists on a tool that does not exist -> attempt errors
    fx(tmp_path, "research", "research", tool_call("bogus_tool", {}))
    fx(tmp_path, "reviewer", "reviewer", verdict(5))

    task_id, state = run(tmp_path, repo, "doomed task")

    assert state["needs_escalation"] is True
    assert "failed twice" in state["escalation_reason"]
    assert state["subtask_results"]["s1"]["error_count"] == 2
    assert repo.tasks[task_id]["status"] == "escalated"


def test_low_plan_confidence_routes_to_escalation_stub(tmp_path, repo):
    fx(tmp_path, "plan", "supervisor",
       plan_text([subtask("s1", "research", "vague thing")], 0.3),
       match=["Create an execution plan"])

    task_id, state = run(tmp_path, repo, "do something vague")

    assert state["needs_escalation"] is True
    assert "confidence" in state["escalation_reason"].lower()
    assert repo.tasks[task_id]["status"] == "escalated"
    assert not state.get("subtask_results")  # no specialist ran


def test_rework_exhaustion_escalates(tmp_path, repo):
    fx(tmp_path, "plan", "supervisor",
       plan_text([subtask("s1", "writing", "draft the memo")], 0.9),
       match=["Create an execution plan"])
    fx(tmp_path, "writing", "writing", final("DRAFT-V1"))
    fx(tmp_path, "reviewer", "reviewer", verdict(2, "still weak"))

    task_id, state = run(tmp_path, repo, "write a memo")

    assert state["needs_escalation"] is True
    assert "below threshold" in state["escalation_reason"]
    assert state["subtask_results"]["s1"]["rework_count"] == 2
    assert repo.tasks[task_id]["status"] == "escalated"


def test_invalid_plan_is_retried_once_with_error_then_fails(tmp_path, repo):
    # First planning call returns a cyclic plan; the retry prompt (which embeds
    # the validation error) returns garbage -> task fails after one retry.
    fx(tmp_path, "a_retry", "supervisor", "still not json",
       match=["Your previous plan was invalid"])
    fx(tmp_path, "b_first", "supervisor",
       plan_text([subtask("s1", "research", "a", depends_on=["s2"]),
                  subtask("s2", "research", "b", depends_on=["s1"])], 0.9),
       match=["Create an execution plan"])

    task_id, state = run(tmp_path, repo, "impossible plan")

    assert state.get("plan") is None
    assert "plan invalid after retry" in state["plan_error"].lower()
    assert repo.tasks[task_id]["status"] == "failed"
