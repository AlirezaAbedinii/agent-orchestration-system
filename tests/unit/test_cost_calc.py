"""Pricing table math and model-name normalization."""

import pytest

from orchestrator.llm.pricing import cost_usd, normalize_model


def test_hand_computed_gpt4o_cost():
    # 1000 prompt tokens * $2.50/M + 500 completion tokens * $10.00/M
    assert cost_usd("gpt-4o", 1000, 500) == pytest.approx(0.0025 + 0.005)


def test_hand_computed_mini_and_sonnet_costs():
    assert cost_usd("gpt-4o-mini", 2_000_000, 1_000_000) == pytest.approx(0.30 + 0.60)
    assert cost_usd("claude-sonnet-5", 100_000, 10_000) == pytest.approx(0.30 + 0.15)


def test_provider_prefix_and_mock_prefix_are_normalized():
    assert normalize_model("openai:gpt-4o") == "gpt-4o"
    assert normalize_model("anthropic:claude-sonnet-5") == "claude-sonnet-5"
    assert normalize_model("mock-gpt-4o") == "gpt-4o"
    # mock fixtures price like the model they stand in for
    assert cost_usd("mock-gpt-4o", 1000, 500) == cost_usd("openai:gpt-4o", 1000, 500)


def test_replay_and_unknown_models_cost_nothing():
    assert cost_usd("replay:gpt-4o", 1_000_000, 1_000_000) == 0.0
    assert cost_usd("mock", 1000, 1000) == 0.0
    assert cost_usd("some-unknown-model", 1000, 1000) == 0.0
