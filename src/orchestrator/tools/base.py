"""Tool framework: specs, context, errors, and the invocation store protocol."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


class ToolError(Exception):
    """Base class for all tool failures."""


class UnknownToolError(ToolError):
    pass


class ToolPermissionError(ToolError):
    pass


class RateLimitExceededError(ToolError):
    pass


class ToolExecutionError(ToolError):
    pass


@dataclass(frozen=True)
class ToolContext:
    task_id: str
    specialist: str
    subtask_id: str | None = None
    workspace: Path = Path("task_workspaces/adhoc")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    owners: frozenset[str]
    rate_limit: int  # max executions per task
    handler: Callable[[BaseModel, ToolContext], BaseModel]
    sensitive: bool = False
    # Per-invocation sensitivity (e.g. api_call POST); overrides `sensitive` when set.
    sensitive_when: Callable[[BaseModel], bool] | None = None

    def is_sensitive(self, args: BaseModel) -> bool:
        if self.sensitive_when is not None:
            return bool(self.sensitive_when(args))
        return self.sensitive


@dataclass
class InvocationRecord:
    task_id: str
    specialist: str
    tool_name: str
    arguments: dict
    status: str  # success | failure | rejected | rate_limited
    latency_ms: float
    subtask_id: str | None = None
    output: dict | None = None
    error: str | None = None
    sensitive: bool = False


class InvocationStore(Protocol):
    def record(self, record: InvocationRecord) -> None: ...

    def count_executions(self, task_id: str, tool_name: str) -> int:
        """Number of attempted executions (success or failure) for rate limiting."""
        ...


@dataclass
class InMemoryInvocationStore:
    """Store used by unit tests; the DB-backed store lives in db/repo.py."""

    records: list[InvocationRecord] = field(default_factory=list)

    def record(self, record: InvocationRecord) -> None:
        self.records.append(record)

    def count_executions(self, task_id: str, tool_name: str) -> int:
        return sum(
            1
            for r in self.records
            if r.task_id == task_id
            and r.tool_name == tool_name
            and r.status in ("success", "failure")
        )
