"""Live smoke test: real provider round-trip for task decomposition.

Runs only with `pytest -m live` and real API keys; deselected in `make test`.
"""

import pytest

from orchestrator.config import get_settings
from orchestrator.planning.decomposer import decompose

pytestmark = pytest.mark.live


@pytest.mark.skipif(
    not get_settings().openai_api_key, reason="OPENAI_API_KEY not configured"
)
def test_live_decomposition_produces_valid_plan():
    from orchestrator.llm.clients import RealLLMClient

    plan = decompose(
        RealLLMClient(),
        "Research the three most popular open-source vector databases, compare their "
        "GitHub activity, and write a one-page recommendation memo.",
    )
    assert len(plan.subtasks) >= 2
    assert 0.0 <= plan.confidence <= 1.0
    assert plan.topological_waves()  # acyclic
