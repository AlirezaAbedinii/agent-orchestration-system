"""Memory retrieval for planning: similar past episodes, facts, preferences.

Retrieved memories are injected into the supervisor's planning prompt in a
labeled block; retrieval counts as access (importance bump) and every
retrieved id is recorded so later phases can trace memory influence.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from orchestrator.config import get_settings
from orchestrator.memory.longterm import KINDS, LongTermMemory
from orchestrator.observability.tracing import child_span, set_attr

# Stable marker; tests assert the planning prompt contains it.
MEMORIES_MARKER = "Relevant memories from past tasks"


@dataclass
class PlanningMemories:
    block: str
    ids: list[str]


def retrieve_for_planning(
    longterm: LongTermMemory,
    *,
    user_id: str,
    request: str,
    k: int | None = None,
    task_id: str | None = None,
    events=None,
) -> PlanningMemories | None:
    k = k or get_settings().memory_retrieval_k
    with child_span("memory:retrieve", kind="memory", user_id=user_id) as span:
        hits = []
        for kind in KINDS:
            hits.extend(longterm.query(kind, request, user_id=user_id, k=k))
        set_attr(span, "retrieved_count", len(hits))
        if not hits:
            return None

        by_kind: dict[str, list[str]] = defaultdict(list)
        for hit in hits:
            by_kind[hit.kind].append(hit.id)
        for kind, ids in by_kind.items():
            longterm.bump_access(kind, ids)  # retrieval is access: importance goes up
            if events is not None:
                for memory_id in ids:
                    events.record(
                        user_id=user_id, memory_id=memory_id, kind=kind, action="retrieved", task_id=task_id
                    )

        set_attr(span, "memory_ids", [hit.id for hit in hits])
        lines = "\n".join(f"- ({hit.kind}) {hit.text}" for hit in hits)
        block = f"{MEMORIES_MARKER} (use them to inform the plan):\n{lines}"
        return PlanningMemories(block=block, ids=[hit.id for hit in hits])
