"""Memory management: importance scoring, consolidation, expiration.

Runs from Celery beat (workers/beat_jobs.py) and from manual maintenance
endpoints (api/routes/memory.py).
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import TYPE_CHECKING

import numpy as np

from orchestrator.config import get_settings
from orchestrator.llm.clients import LLMClient

if TYPE_CHECKING:  # longterm imports compute_importance; avoid a circular import
    from orchestrator.memory.longterm import LongTermMemory


def compute_importance(
    access_count: int,
    last_accessed_at: float,
    *,
    now: float | None = None,
    half_life_days: float | None = None,
) -> float:
    """Frequently accessed memories matter more; stale ones decay.

    importance = (1 + access_count) * 0.5 ** (days_since_last_access / half_life)
    """
    settings = get_settings()
    now = now if now is not None else time.time()
    half_life = half_life_days if half_life_days is not None else settings.memory_half_life_days
    age_days = max(0.0, (now - last_accessed_at) / 86_400)
    return (1 + access_count) * 0.5 ** (age_days / half_life)


# Stable marker; mock fixtures match on it.
CONSOLIDATE_MARKER = "Consolidate the following memories"

CONSOLIDATE_PROMPT = """{marker} into ONE higher-level memory that preserves every distinct detail.

Memories:
{texts}

Respond with only the consolidated memory text, no preamble.
"""


def _clusters(similarities: np.ndarray, threshold: float) -> list[list[int]]:
    """Connected components over the pairs with similarity >= threshold."""
    n = similarities.shape[0]
    visited = [False] * n
    components = []
    for start in range(n):
        if visited[start]:
            continue
        stack, component = [start], []
        while stack:
            node = stack.pop()
            if visited[node]:
                continue
            visited[node] = True
            component.append(node)
            stack.extend(
                other
                for other in range(n)
                if not visited[other] and similarities[node, other] >= threshold
            )
        components.append(sorted(component))
    return components


def consolidate(
    longterm: LongTermMemory,
    llm: LLMClient,
    *,
    user_id: str | None = None,
    threshold: float | None = None,
    events=None,
) -> dict:
    """Merge near-duplicate memories into higher-level summaries."""
    from orchestrator.memory.longterm import KINDS

    threshold = threshold or get_settings().memory_consolidation_similarity
    report: dict = {"clusters_merged": 0, "created": [], "deleted": []}
    for kind in KINDS:
        result = longterm.all_items(kind, user_id=user_id, with_embeddings=True)
        ids, docs, metas = result["ids"], result["documents"], result["metadatas"]
        if len(ids) < 2:
            continue
        embeddings = np.asarray(result["embeddings"], dtype=float)

        by_user: dict[str, list[int]] = defaultdict(list)
        for index, meta in enumerate(metas):
            by_user[meta.get("user_id", "default")].append(index)

        for owner, indices in by_user.items():
            if len(indices) < 2:
                continue
            matrix = embeddings[indices]
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            matrix = matrix / norms
            similarities = matrix @ matrix.T

            for component in _clusters(similarities, threshold):
                if len(component) < 2:
                    continue
                members = [indices[i] for i in component]
                member_ids = [ids[i] for i in members]
                bullets = "\n".join(f"- {docs[i]}" for i in members)
                summary = llm.complete(
                    "memory", CONSOLIDATE_PROMPT.format(marker=CONSOLIDATE_MARKER, texts=bullets)
                ).text.strip()

                now = time.time()
                total_access = sum(int(metas[i].get("access_count", 0)) for i in members)
                merged_id = longterm.add(
                    kind,
                    summary,
                    user_id=owner,
                    extra={
                        "access_count": total_access,
                        "importance": compute_importance(total_access, now, now=now),
                        "consolidated_from": ",".join(member_ids),
                    },
                )
                longterm.delete(kind, member_ids)
                if events is not None:
                    events.record(
                        user_id=owner, memory_id=merged_id, kind=kind, action="consolidated",
                        detail=f"merged {len(member_ids)} memories: {','.join(member_ids)}",
                    )
                    for old_id in member_ids:
                        events.record(
                            user_id=owner, memory_id=old_id, kind=kind, action="deleted",
                            detail=f"consolidated into {merged_id}",
                        )
                report["clusters_merged"] += 1
                report["created"].append({"kind": kind, "id": merged_id})
                report["deleted"].extend({"kind": kind, "id": old} for old in member_ids)
    return report


def expire(
    longterm: LongTermMemory,
    *,
    user_id: str | None = None,
    now: float | None = None,
    events=None,
) -> dict:
    """Recompute importance everywhere; delete stale, unimportant memories."""
    from orchestrator.memory.longterm import KINDS

    settings = get_settings()
    now = now if now is not None else time.time()
    report: dict = {"expired": [], "recomputed": 0}
    for kind in KINDS:
        result = longterm.all_items(kind, user_id=user_id)
        expired_ids, keep_ids, keep_metas = [], [], []
        for memory_id, meta in zip(result["ids"], result["metadatas"]):
            last = float(meta.get("last_accessed_at", meta.get("created_at", now)))
            importance = compute_importance(int(meta.get("access_count", 0)), last, now=now)
            age_days = (now - last) / 86_400
            if importance < settings.memory_min_importance and age_days > settings.memory_expiry_days:
                expired_ids.append(memory_id)
                if events is not None:
                    events.record(
                        user_id=meta.get("user_id", "default"), memory_id=memory_id, kind=kind,
                        action="expired", detail=f"importance {importance:.3f} after {age_days:.0f}d",
                    )
            else:
                meta["importance"] = importance
                keep_ids.append(memory_id)
                keep_metas.append(meta)
        if keep_ids:
            longterm.set_metadata(kind, keep_ids, keep_metas)
            report["recomputed"] += len(keep_ids)
        if expired_ids:
            longterm.delete(kind, expired_ids)
            report["expired"].extend({"kind": kind, "id": mid} for mid in expired_ids)
    return report
