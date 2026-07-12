"""Integration test setup: requires the compose infra services (make infra).

Applies migrations, seeds the demo schema, and truncates state between tests.
Skips the whole suite gracefully when Postgres is unreachable.
"""

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from orchestrator.db.session import get_engine


def _postgres_up() -> bool:
    try:
        with get_engine().connect() as connection:
            connection.execute(sa.text("SELECT 1"))
        return True
    except Exception:
        return False


POSTGRES_UP = _postgres_up()


@pytest.fixture(scope="session", autouse=True)
def _database():
    if not POSTGRES_UP:
        pytest.skip("postgres is not reachable; run `make infra` first")
    command.upgrade(Config("alembic.ini"), "head")
    from orchestrator.db.seed_demo_data import seed

    seed()
    yield


@pytest.fixture(autouse=True)
def _clean_tables(_database):
    engine = get_engine()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "TRUNCATE approvals, tool_invocations, subtasks, plans, tasks, memory_events CASCADE"
        )
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(f'TRUNCATE "{table}"')
        except Exception:
            pass  # checkpointer tables appear on first graph build
    _purge_redis()
    _purge_chroma()
    yield


def _purge_redis() -> None:
    import redis

    from orchestrator.config import get_settings

    client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    for key in client.scan_iter(match="task:*"):
        client.delete(key)


def _purge_chroma() -> None:
    """Delete memory items (not collections — live handles cache collection ids)."""
    from orchestrator.memory.longterm import KINDS, LongTermMemory

    longterm = LongTermMemory()
    for kind in KINDS:
        result = longterm.all_items(kind)
        if result["ids"]:
            longterm.delete(kind, result["ids"])
