import json

import pytest

from orchestrator.llm.mock import FixtureNotFoundError, MockLLMClient, fixture_key


def _write_fixture(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_plays_back_exact_recorded_call(tmp_path):
    prompt = "Plan the following task: compare vector databases"
    key = fixture_key("supervisor", prompt)
    _write_fixture(
        tmp_path / f"{key}.json",
        {
            "agent": "supervisor",
            "prompt": prompt,
            "response": {"text": "PLAN", "model": "gpt-4o", "prompt_tokens": 10, "completion_tokens": 5},
        },
    )

    response = MockLLMClient(tmp_path).complete("supervisor", prompt)
    assert response.text == "PLAN"
    assert response.model == "gpt-4o"
    assert (response.prompt_tokens, response.completion_tokens) == (10, 5)


def test_falls_back_to_agent_default(tmp_path):
    _write_fixture(
        tmp_path / "reviewer.json",
        {"agent": "reviewer", "response": {"text": "APPROVED"}},
    )

    response = MockLLMClient(tmp_path).complete("reviewer", "any prompt at all")
    assert response.text == "APPROVED"
    assert response.model == "mock"


def test_missing_fixture_raises_with_key(tmp_path):
    with pytest.raises(FixtureNotFoundError) as excinfo:
        MockLLMClient(tmp_path).complete("writer", "unrecorded prompt")
    assert fixture_key("writer", "unrecorded prompt") in str(excinfo.value)
    assert "writer.json" in str(excinfo.value)
