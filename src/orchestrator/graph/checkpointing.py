"""Postgres checkpointer for graph state (pause/resume arrives in Phase 3)."""

from __future__ import annotations

from functools import lru_cache

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row

from orchestrator.config import get_settings


@lru_cache
def get_checkpointer() -> PostgresSaver:
    url = get_settings().database_url.replace("+psycopg", "")
    connection = Connection.connect(url, autocommit=True, prepare_threshold=0, row_factory=dict_row)
    saver = PostgresSaver(connection)
    saver.setup()
    return saver
