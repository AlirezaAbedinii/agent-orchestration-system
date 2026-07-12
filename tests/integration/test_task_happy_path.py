"""End-to-end happy path through the API with MOCK_LLM fixtures.

Covers plan validity, all four specialists using their tools, parallel wave
batching, DB persistence, and the Postgres checkpointer.
"""

import sqlalchemy as sa

from orchestrator.config import get_settings
from orchestrator.db.session import get_engine

REQUEST = (
    "Compare open-source vector databases: gather facts about Chroma from the web, "
    "compute the GitHub star ranking from the demo database, generate a comparison "
    "table using Python, and write a comparison memo saved as memo.md."
)


def test_happy_path_full_lifecycle(client):
    created = client.post("/tasks", json={"request": REQUEST})
    assert created.status_code == 202
    task_id = created.json()["task_id"]

    bundle = client.get(f"/tasks/{task_id}").json()

    # plan validity (acceptance: >=2 subtasks, valid specialists, confidence in [0,1])
    assert bundle["status"] == "completed"
    plan = bundle["plan"]
    assert len(plan["subtasks"]) >= 2
    assert 0.0 <= plan["confidence"] <= 1.0
    assert {s["specialist"] for s in plan["subtasks"]} == {"research", "analysis", "code", "writing"}

    # every subtask completed and reviewed
    assert all(s["status"] == "completed" for s in bundle["subtasks"])
    assert all((s["review_score"] or 0) >= 3 for s in bundle["subtasks"])
    assert bundle["final_output"].startswith("FINAL")

    # each specialist used at least one of its own tools (logged invocations)
    with get_engine().connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT specialist, tool_name FROM tool_invocations "
                "WHERE task_id = :t AND status = 'success'"
            ),
            {"t": task_id},
        ).all()
    used = {(specialist, tool) for specialist, tool in rows}
    assert {"research", "analysis", "code", "writing"} <= {s for s, _ in used}
    assert {("research", "web_search"), ("analysis", "db_query"),
            ("code", "code_exec"), ("writing", "file_write")} <= used

    # parallel dispatch: the first scheduler wave batched the 3 independent subtasks
    from orchestrator.graph.runner import get_production_graph

    state = get_production_graph().get_state({"configurable": {"thread_id": task_id}})
    dispatch_log = state.values["dispatch_log"]
    assert dispatch_log[0] == ["s1", "s2", "s3"]
    assert dispatch_log[1] == ["s4"]

    # the writing specialist really wrote the memo into the task workspace
    memo = get_settings().workspace_root / task_id / "memo.md"
    assert memo.is_file()
    assert "Vector DB comparison" in memo.read_text(encoding="utf-8")


def test_get_unknown_task_is_404(client):
    assert client.get("/tasks/does-not-exist").status_code == 404
