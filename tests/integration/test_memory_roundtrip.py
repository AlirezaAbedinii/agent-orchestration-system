"""Memory roundtrip: working memory grows during a run and vanishes after,
completion extracts memories into every applicable ChromaDB collection, and
memory events are recorded.
"""

from orchestrator.db.repo import DBTaskRepo, MemoryEventStore
from orchestrator.graph.runner import get_production_graph
from orchestrator.memory.longterm import LongTermMemory
from orchestrator.memory.working import WorkingMemory

REQUEST = (
    "Compare open-source vector databases: gather facts about Chroma from the web, "
    "compute the GitHub star ranking from the demo database, generate a comparison "
    "table using Python, and write a comparison memo saved as memo.md."
)


def test_working_memory_lifecycle_and_extraction():
    repo = DBTaskRepo()
    task_id = repo.create_task(REQUEST, user_id="default")
    graph = get_production_graph()
    working = WorkingMemory()

    plan_in_memory = False
    output_counts: list[int] = []
    stream = graph.stream(
        {
            "task_id": task_id,
            "request": REQUEST,
            "user_id": "default",
            "subtask_results": {},
            "dispatch_log": [],
        },
        {"configurable": {"thread_id": task_id}},
        stream_mode="updates",
    )
    for update in stream:
        node = next(iter(update))
        if node == "plan":
            plan_in_memory = working.get_plan(task_id) is not None
        if node == "gather":
            output_counts.append(len(working.get_subtask_outputs(task_id)))

    # the plan landed in working memory, outputs grew wave by wave, then cleared
    assert plan_in_memory
    assert output_counts == sorted(output_counts) and output_counts[-1] == 4
    assert output_counts[0] < output_counts[-1]
    assert not working.exists(task_id)

    assert repo.get_task(task_id)["status"] == "completed"

    # extraction stored at least one memory per applicable collection
    memories = LongTermMemory().get_all("default")
    assert len(memories["episodes"]) >= 1
    assert len(memories["facts"]) >= 1
    assert len(memories["preferences"]) >= 1
    assert all(item["task_id"] == task_id for item in memories["episodes"])

    # every stored memory was audited
    events = MemoryEventStore().recent("default", limit=50)
    created = [e for e in events if e["action"] == "created"]
    total = sum(len(v) for v in memories.values())
    assert len(created) == total


def test_dashboard_and_user_data_deletion(client):
    longterm = LongTermMemory()
    working = WorkingMemory()
    events = MemoryEventStore()
    memory_id = longterm.add("facts", "Alice tracks vector database releases", user_id="alice")
    longterm.add("preferences", "Alice prefers concise memos", user_id="alice")
    working.start("task-alice-1", "alice")
    events.record(user_id="alice", memory_id=memory_id, kind="facts", action="created")

    dashboard = client.get("/memory/users/alice").json()
    assert dashboard["counts"]["facts"] == 1
    assert dashboard["counts"]["preferences"] == 1
    assert dashboard["collections"]["facts"][0]["text"] == "Alice tracks vector database releases"
    assert any(e["action"] == "created" for e in dashboard["recent_events"])

    deleted = client.delete("/memory/users/alice").json()
    assert deleted["deleted"]["long_term"]["facts"] == 1
    assert deleted["deleted"]["working_memory_tasks"] == 1
    assert deleted["deleted"]["audit_events"] == 1

    # zero ChromaDB entries and zero Redis keys remain for that user
    remaining = longterm.get_all("alice")
    assert sum(len(items) for items in remaining.values()) == 0
    assert not working.exists("task-alice-1")
    assert client.get("/memory/users/alice").json()["recent_events"] == []
