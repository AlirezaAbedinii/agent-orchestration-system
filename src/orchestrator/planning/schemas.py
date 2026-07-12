"""ExecutionPlan / Subtask schemas (PROJECT_IMPLEMENTATION_PLAN.md §5 Phase 1.2).

The supervisor emits these via structured output. Validation enforces unique
subtask ids, resolvable dependencies, and an acyclic dependency graph.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Specialist = Literal["research", "analysis", "writing", "code"]
Complexity = Literal["low", "medium", "high"]


class Subtask(BaseModel):
    id: str = Field(min_length=1, description="Plan-local id, e.g. 's1'")
    description: str = Field(min_length=1)
    specialist: Specialist
    required_inputs: list[str] = Field(
        default_factory=list, description="What this subtask needs to start (free text)"
    )
    expected_output_format: str = "plain text"
    estimated_complexity: Complexity = "medium"
    depends_on: list[str] = Field(
        default_factory=list, description="Ids of subtasks whose outputs feed this one"
    )


class ExecutionPlan(BaseModel):
    task_summary: str = ""
    subtasks: list[Subtask] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_dag(self) -> "ExecutionPlan":
        ids = [s.id for s in self.subtasks]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Duplicate subtask ids: {ids}")
        known = set(ids)
        for subtask in self.subtasks:
            unknown = [dep for dep in subtask.depends_on if dep not in known]
            if unknown:
                raise ValueError(f"Subtask {subtask.id!r} depends on unknown ids {unknown}")
            if subtask.id in subtask.depends_on:
                raise ValueError(f"Subtask {subtask.id!r} depends on itself")
        self.topological_waves()  # raises on cycles
        return self

    def subtask(self, sid: str) -> Subtask:
        return next(s for s in self.subtasks if s.id == sid)

    def topological_waves(self) -> list[list[str]]:
        """Kahn's algorithm returning parallel-executable batches of subtask ids."""
        pending = {s.id: set(s.depends_on) for s in self.subtasks}
        waves: list[list[str]] = []
        while pending:
            ready = sorted(sid for sid, deps in pending.items() if not deps)
            if not ready:
                raise ValueError(f"Dependency cycle among subtasks: {sorted(pending)}")
            waves.append(ready)
            for sid in ready:
                del pending[sid]
            for deps in pending.values():
                deps.difference_update(ready)
        return waves
