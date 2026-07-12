"""Working memory behaviour and its wiring into the task graph.

Uses the dict-backed InMemoryWorkingMemory (same interface as the Redis
implementation, which the integration suite exercises against a live server).
"""

import json

from orchestrator.db.repo import InMemoryTaskRepo
from orchestrator.graph.builder import build_graph
from orchestrator.llm.mock import MockLLMClient
from orchestrator.memory.working import InMemoryWorkingMemory
from orchestrator.tools.base import InMemoryInvocationStore
from orchestrator.tools.defaults import build_default_registry


def test_plan_roundtrip_and_user():
    memory = InMemoryWorkingMemory()
    memory.start("t1", "alice")
    memory.set_plan("t1", {"subtasks": [{"id": "s1"}]})
    assert memory.get_plan("t1") == {"subtasks": [{"id": "s1"}]}
    assert memory.get_user("t1") == "alice"
    assert memory.get_plan("t2") is None


def test_outputs_grow_and_errors_accumulate():
    memory = InMemoryWorkingMemory()
    memory.start("t1", "alice")
    memory.record_subtask_output("t1", "s1", "OUT-1")
    assert memory.get_subtask_outputs("t1") == {"s1": "OUT-1"}
    memory.record_subtask_output("t1", "s2", "OUT-2")
    assert memory.get_subtask_outputs("t1") == {"s1": "OUT-1", "s2": "OUT-2"}
    memory.record_error("t1", "s1", "boom")
    memory.record_error("t1", "s2", "bang")
    assert [e["error"] for e in memory.get_errors("t1")] == ["boom", "bang"]
    memory.set_intermediate("t1", "tools:s1", ["web_search"])
    assert memory.get_intermediates("t1") == {"tools:s1": ["web_search"]}


def test_clear_scopes_to_one_task():
    memory = InMemoryWorkingMemory()
    memory.start("t1", "alice")
    memory.start("t2", "alice")
    memory.clear("t1")
    assert not memory.exists("t1")
    assert memory.exists("t2")


def test_clear_user_only_touches_that_user():
    memory = InMemoryWorkingMemory()
    memory.start("t1", "alice")
    memory.start("t2", "bob")
    memory.start("t3", "alice")
    assert memory.clear_user("alice") == 2
    assert not memory.exists("t1") and not memory.exists("t3")
    assert memory.exists("t2")


class RecordingWorkingMemory(InMemoryWorkingMemory):
    """Captures write calls so the graph-wiring test can assert on them."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []

    def start(self, task_id, user_id):
        self.calls.append(("start", task_id, user_id))
        super().start(task_id, user_id)

    def set_plan(self, task_id, plan):
        self.calls.append(("set_plan", task_id))
        super().set_plan(task_id, plan)

    def record_subtask_output(self, task_id, sid, output):
        self.calls.append(("output", task_id, sid))
        super().record_subtask_output(task_id, sid, output)

    def clear(self, task_id):
        self.calls.append(("clear", task_id))
        super().clear(task_id)


def _fx(directory, name, agent, text, match=None):
    payload = {"agent": agent, "response": {"text": text}}
    if match:
        payload["match"] = match
    (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_graph_shares_and_clears_working_memory(tmp_path):
    plan = {
        "task_summary": "t",
        "confidence": 0.9,
        "subtasks": [
            {"id": "s1", "description": "look up A", "specialist": "research",
             "required_inputs": [], "expected_output_format": "plain text",
             "estimated_complexity": "low", "depends_on": []},
            {"id": "s2", "description": "summarize", "specialist": "writing",
             "required_inputs": [], "expected_output_format": "plain text",
             "estimated_complexity": "low", "depends_on": ["s1"]},
        ],
    }
    _fx(tmp_path, "plan", "supervisor", json.dumps(plan), match=["Create an execution plan"])
    _fx(tmp_path, "synth", "supervisor", "DONE", match=["Synthesize the final deliverable"])
    _fx(tmp_path, "research", "research", json.dumps({"action": "final", "output": "A-FACTS"}))
    _fx(tmp_path, "writing", "writing", json.dumps({"action": "final", "output": "SUMMARY"}))
    _fx(tmp_path, "reviewer", "reviewer", json.dumps({"score": 5, "feedback": ""}))

    memory = RecordingWorkingMemory()
    repo = InMemoryTaskRepo()
    graph = build_graph(
        llm=MockLLMClient(tmp_path),
        registry=build_default_registry(InMemoryInvocationStore()),
        repo=repo,
        working=memory,
    )
    task_id = repo.create_task("look up A and summarize", user_id="alice")
    graph.invoke({"task_id": task_id, "request": "look up A and summarize",
                  "user_id": "alice", "subtask_results": {}, "dispatch_log": []})

    assert ("start", task_id, "alice") in memory.calls
    assert ("set_plan", task_id) in memory.calls
    assert ("output", task_id, "s1") in memory.calls
    assert ("output", task_id, "s2") in memory.calls
    # dependent subtask ran after its input was in working memory
    assert memory.calls.index(("output", task_id, "s1")) < memory.calls.index(("output", task_id, "s2"))
    # cleared on completion
    assert memory.calls[-1] == ("clear", task_id)
    assert not memory.exists(task_id)
