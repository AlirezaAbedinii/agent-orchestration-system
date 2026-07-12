"""Read-only SQL tool against the seeded `demo` schema in Postgres."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, Field

from orchestrator.db.session import get_engine
from orchestrator.tools.base import ToolContext, ToolExecutionError, ToolSpec

_ALLOWED_PREFIXES = ("SELECT", "WITH")


class DbQueryInput(BaseModel):
    sql: str = Field(min_length=1, description="A single read-only SELECT statement")
    max_rows: int = Field(default=50, ge=1, le=200)


class DbQueryOutput(BaseModel):
    rows: list[dict]
    row_count: int


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def handle(args: DbQueryInput, ctx: ToolContext) -> DbQueryOutput:
    statement = args.sql.strip().rstrip(";").strip()
    if ";" in statement:
        raise ToolExecutionError("Only a single SQL statement is allowed")
    if not statement.upper().startswith(_ALLOWED_PREFIXES):
        raise ToolExecutionError("Only read-only SELECT/WITH queries are allowed")

    engine = get_engine()
    with engine.begin() as connection:
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        result = connection.execute(sa.text(statement))
        rows = [
            {key: _jsonable(value) for key, value in mapping.items()}
            for mapping in result.mappings().fetchmany(args.max_rows)
        ]
    return DbQueryOutput(rows=rows, row_count=len(rows))


SPEC = ToolSpec(
    name="db_query",
    description="Run a read-only SQL SELECT against the demo database schema",
    input_schema=DbQueryInput,
    output_schema=DbQueryOutput,
    owners=frozenset({"analysis"}),
    rate_limit=10,
    handler=handle,
)
