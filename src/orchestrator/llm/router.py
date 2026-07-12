"""Agent → provider/model routing (PROJECT_IMPLEMENTATION_PLAN.md §3.2).

The supervisor gets a strong model, specialists a cheaper one, and the reviewer
is always routed to a *different provider* than the agent whose output it
reviews — that cross-provider check is what implements the plan's
"multi-model agent routing".
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.config import get_settings

SPECIALISTS = ("research", "analysis", "writing", "code")

# Fallback model per provider, used when the reviewer must switch provider.
_PROVIDER_DEFAULTS = {
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-5",
}


@dataclass(frozen=True)
class ModelChoice:
    provider: str
    model: str


def _parse(spec: str) -> ModelChoice:
    provider, _, model = spec.partition(":")
    if provider not in _PROVIDER_DEFAULTS or not model:
        raise ValueError(f"Invalid model spec {spec!r}; expected '<openai|anthropic>:<model>'")
    return ModelChoice(provider, model)


def route(agent: str, producer_provider: str | None = None) -> ModelChoice:
    """Return the provider/model for *agent*.

    ``producer_provider`` is only meaningful for the reviewer: it is the
    provider that generated the output under review, and the reviewer is moved
    off it if the configured reviewer model would collide.
    """
    settings = get_settings()
    if agent == "supervisor":
        return _parse(settings.model_supervisor)
    if agent in SPECIALISTS:
        return _parse(settings.model_specialist)
    if agent == "memory":
        return _parse(settings.model_memory)
    if agent == "hitl":  # reviewer-facing chat about a paused task
        return _parse(settings.model_supervisor)
    if agent == "reviewer":
        choice = _parse(settings.model_reviewer)
        if producer_provider and choice.provider == producer_provider:
            other = next(p for p in _PROVIDER_DEFAULTS if p != producer_provider)
            return ModelChoice(other, _PROVIDER_DEFAULTS[other])
        return choice
    raise ValueError(f"Unknown agent {agent!r}")
