"""Graph conditional-edge tests: run the full LangGraph with scripted mock
fixtures, an in-memory repo, queue, and invocation store (no Postgres).
Escalation tests use MemorySaver so interrupts can pause and resume."""

import json

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from orchestrator.db.repo import InMemoryTaskRepo
from orchestrator.graph.builder import build_graph
from orchestrator.hitl.queue import InMemoryApprovalQueue
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


def run_until_pause(tmp_path, repo, request_text: str, queue):
    """Build an interrupt-capable graph (MemorySaver) and run to the pause."""
    graph = build_graph(
        llm=MockLLMClient(tmp_path),
        registry=build_default_registry(InMemoryInvocationStore()),
        repo=repo,
        checkpointer=MemorySaver(),
        approvals=queue,
    )
    task_id = repo.create_task(request_text)
    config = {"configurable": {"thread_id": task_id}}
    state = graph.invoke(
        {"task_id": task_id, "request": request_text, "subtask_results": {}, "dispatch_log": []},
        config,
    )
    return task_id, state, graph, config


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


def test_two_specialist_failures_pause_then_human_takes_over(tmp_path, repo):
    fx(tmp_path, "plan", "supervisor",
       plan_text([subtask("s1", "research", "look something up")], 0.9),
       match=["Create an execution plan"])
    fx(tmp_path, "synth", "supervisor", "DONE", match=["Synthesize the final deliverable"])
    # the specialist insists on a tool that does not exist -> attempt errors
    fx(tmp_path, "research", "research", tool_call("bogus_tool", {}))
    fx(tmp_path, "reviewer", "reviewer", verdict(5))

    queue = InMemoryApprovalQueue()
    task_id, state, graph, config = run_until_pause(tmp_path, repo, "doomed task", queue)

    interrupt_payload = state["__interrupt__"][0].value
    assert interrupt_payload["trigger"] == "specialist_double_failure"
    assert state["subtask_results"]["s1"]["error_count"] == 2
    (row,) = queue.list(status="pending")
    assert row["level"] == "approve_action" and row["gate_key"].startswith("subtask:s1")
    assert repo.tasks[task_id]["status"] == "awaiting_approval"

    finished = graph.invoke(
        Command(resume={"action": "take_over", "payload": {"output": "HUMAN-OUT"},
                        "notes": "did it myself"}),
        config,
    )
    assert finished["subtask_results"]["s1"]["status"] == "completed"
    assert finished["subtask_results"]["s1"]["output"] == "HUMAN-OUT"
    assert finished["subtask_results"]["s1"]["human_provided"] is True
    assert finished["final_output"] == "DONE"
    assert repo.tasks[task_id]["status"] == "completed"
    assert repo.subtasks[task_id]["s1"]["output"] == "HUMAN-OUT"


def test_low_plan_confidence_pauses_then_approve_resumes(tmp_path, repo):
    fx(tmp_path, "plan", "supervisor",
       plan_text([subtask("s1", "research", "vague thing")], 0.3),
       match=["Create an execution plan"])
    fx(tmp_path, "synth", "supervisor", "DONE", match=["Synthesize the final deliverable"])
    fx(tmp_path, "research", "research", final("RESEARCH-OK"))
    fx(tmp_path, "reviewer", "reviewer", verdict(5))

    queue = InMemoryApprovalQueue()
    task_id, state, graph, config = run_until_pause(tmp_path, repo, "do something vague", queue)

    interrupt_payload = state["__interrupt__"][0].value
    assert interrupt_payload["trigger"] == "low_plan_confidence"
    assert interrupt_payload["level"] == "approve_plan"
    assert interrupt_payload["proposed_action"]["type"] == "execute_plan"
    (row,) = queue.list(status="pending")
    assert row["gate_key"] == "plan"
    assert repo.tasks[task_id]["status"] == "awaiting_approval"
    assert not state.get("subtask_results")  # nothing executed while pending

    finished = graph.invoke(Command(resume={"action": "approve", "payload": {}, "notes": "go"}), config)
    assert finished["final_output"] == "DONE"
    assert repo.tasks[task_id]["status"] == "completed"


def test_rework_exhaustion_pauses_then_reject_terminates(tmp_path, repo):
    fx(tmp_path, "plan", "supervisor",
       plan_text([subtask("s1", "writing", "draft the memo")], 0.9),
       match=["Create an execution plan"])
    fx(tmp_path, "writing", "writing", final("DRAFT-V1"))
    fx(tmp_path, "reviewer", "reviewer", verdict(2, "still weak"))

    queue = InMemoryApprovalQueue()
    task_id, state, graph, config = run_until_pause(tmp_path, repo, "write a memo", queue)

    interrupt_payload = state["__interrupt__"][0].value
    assert interrupt_payload["trigger"] == "low_review_score"
    assert state["subtask_results"]["s1"]["rework_count"] == 2

    finished = graph.invoke(
        Command(resume={"action": "reject", "payload": {}, "notes": "not salvageable"}), config
    )
    assert repo.tasks[task_id]["status"] == "rejected"
    assert "not salvageable" in repo.tasks[task_id]["error"]
    assert not finished.get("final_output")


def test_notify_level_records_but_never_pauses(tmp_path, repo, monkeypatch):
    from orchestrator.config import get_settings

    monkeypatch.setitem(get_settings().approval_level_overrides, "low_plan_confidence", "notify")
    fx(tmp_path, "plan", "supervisor",
       plan_text([subtask("s1", "research", "vague thing")], 0.3),
       match=["Create an execution plan"])
    fx(tmp_path, "synth", "supervisor", "DONE", match=["Synthesize the final deliverable"])
    fx(tmp_path, "research", "research", final("RESEARCH-OK"))
    fx(tmp_path, "reviewer", "reviewer", verdict(5))

    queue = InMemoryApprovalQueue()
    task_id, state, graph, config = run_until_pause(tmp_path, repo, "do something vague", queue)

    assert "__interrupt__" not in state  # proceeded without pausing
    assert state["final_output"] == "DONE"
    assert queue.list(status="pending") == []
    (row,) = queue.list(status="notified")
    assert row["trigger"] == "low_plan_confidence"
    assert repo.tasks[task_id]["status"] == "completed"


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
