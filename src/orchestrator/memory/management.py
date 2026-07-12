"""Memory importance scoring (consolidation and expiration build on this)."""

from __future__ import annotations

import time

from orchestrator.config import get_settings


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
