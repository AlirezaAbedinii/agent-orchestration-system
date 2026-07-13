"""E2E 2 (plan §7): specialists correctly use their tools.

Every specialist that ran logged at least one invocation, every invocation
respected tool ownership, and inputs/outputs were captured.
"""

import sqlalchemy as sa

from orchestrator.db.session import get_engine

VECTOR_REQUEST = (
    "Compare open-source vector databases: gather facts about Chroma from the web, "
    "compute the GitHub star ranking from the demo database, generate a comparison "
    "table using Python, and write a comparison memo saved as memo.md."
)

# Mirror of the registry's ownership map (tools/*.py SPEC owners).
OWNERS = {
    "web_search": {"research"},
    "api_call": {"research"},
    "db_query": {"analysis"},
    "code_exec": {"analysis", "code"},
    "file_read": {"analysis", "writing", "code"},
    "file_write": {"analysis", "writing", "code"},
}


def test_each_specialist_uses_only_its_own_tools_with_logged_io(client):
    task_id = client.post("/tasks", json={"request": VECTOR_REQUEST}).json()["task_id"]
    assert client.get(f"/tasks/{task_id}").json()["status"] == "completed"

    with get_engine().connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT specialist, tool_name, status, arguments, output, latency_ms "
                "FROM tool_invocations WHERE task_id = :t"
            ),
            {"t": task_id},
        ).mappings().all()

    assert rows, "no tool invocations were logged"

    # all four specialists used tools, including their signature tool
    assert {row["specialist"] for row in rows} == {"research", "analysis", "code", "writing"}
    used = {(row["specialist"], row["tool_name"]) for row in rows if row["status"] == "success"}
    assert {
        ("research", "web_search"),
        ("analysis", "db_query"),
        ("code", "code_exec"),
        ("writing", "file_write"),
    } <= used

    # ownership held on every single invocation, and I/O was logged
    for row in rows:
        assert row["specialist"] in OWNERS[row["tool_name"]], (
            f"{row['specialist']} used non-owned tool {row['tool_name']}"
        )
        assert row["arguments"] is not None
        if row["status"] == "success":
            assert row["output"] is not None
        assert row["latency_ms"] >= 0
