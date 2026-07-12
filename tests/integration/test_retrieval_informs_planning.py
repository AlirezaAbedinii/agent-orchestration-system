"""Run task A, then a similar task A′: A′'s planning prompt must contain a
memory extracted from A (asserted on the actual prompt), and the retrieved
memory ids must be recorded.
"""

from pathlib import Path

from orchestrator.db.repo import DBInvocationStore, DBTaskRepo, MemoryEventStore
from orchestrator.graph.builder import build_graph
from orchestrator.llm.mock import MockLLMClient
from orchestrator.memory.longterm import LongTermMemory
from orchestrator.memory.retrieval import MEMORIES_MARKER
from orchestrator.memory.working import WorkingMemory
from orchestrator.planning.decomposer import PLAN_MARKER
from orchestrator.tools.defaults import build_default_registry

REQUEST_A = (
    "Compare open-source vector databases: gather facts about Chroma from the web, "
    "compute the GitHub star ranking from the demo database, generate a comparison "
    "table using Python, and write a comparison memo saved as memo.md."
)
REQUEST_B = (
    "Compare open-source vector databases again for a new report: gather facts about "
    "Chroma from the web, compute the GitHub star ranking from the demo database, "
    "generate a comparison table using Python, and write a comparison memo saved as memo.md."
)


class RecordingLLM:
    """Wraps the mock client and captures every (agent, prompt) pair."""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[tuple[str, str]] = []

    def complete(self, agent, prompt, *, producer_provider=None):
        self.calls.append((agent, prompt))
        return self.inner.complete(agent, prompt, producer_provider=producer_provider)


def _planning_prompt(calls) -> str:
    return next(p for agent, p in calls if agent == "supervisor" and PLAN_MARKER in p)


def test_second_similar_task_plans_with_retrieved_memory():
    llm = RecordingLLM(MockLLMClient(Path("tests/fixtures/llm")))
    repo = DBTaskRepo()
    graph = build_graph(
        llm=llm,
        registry=build_default_registry(DBInvocationStore()),
        repo=repo,
        working=WorkingMemory(),
        longterm=LongTermMemory(),
        memory_events=MemoryEventStore(),
    )

    def run(task_id: str, request: str):
        return graph.invoke(
            {"task_id": task_id, "request": request, "user_id": "default",
             "subtask_results": {}, "dispatch_log": []}
        )

    # task A: no memories exist yet, so planning sees none
    task_a = repo.create_task(REQUEST_A, user_id="default")
    state_a = run(task_a, REQUEST_A)
    assert repo.get_task(task_a)["status"] == "completed"
    assert MEMORIES_MARKER not in _planning_prompt(llm.calls)
    assert state_a["retrieved_memory_ids"] == []

    # task A': similar request — planning must be informed by A's memories
    llm.calls.clear()
    task_b = repo.create_task(REQUEST_B, user_id="default")
    state_b = run(task_b, REQUEST_B)
    assert repo.get_task(task_b)["status"] == "completed"

    prompt = _planning_prompt(llm.calls)
    assert MEMORIES_MARKER in prompt
    # a memory extracted from task A, verbatim, inside the planning prompt
    assert "Chroma is an open-source embedding database" in prompt

    # retrieved ids recorded: in state and in the audit log, tied to task B
    assert state_b["retrieved_memory_ids"]
    events = MemoryEventStore().recent("default", limit=100)
    retrieved_for_b = {
        e["memory_id"] for e in events if e["action"] == "retrieved" and e["task_id"] == task_b
    }
    assert retrieved_for_b == set(state_b["retrieved_memory_ids"])

    # retrieval counts as access: importance/access_count went up on a hit
    memories = LongTermMemory().get_all("default")
    accessed = [
        item
        for group in memories.values()
        for item in group
        if item["id"] in retrieved_for_b and item["access_count"] >= 1
    ]
    assert accessed
