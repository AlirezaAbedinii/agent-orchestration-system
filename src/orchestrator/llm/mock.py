"""Fixture-playback LLM client, active when MOCK_LLM=1.

Recorded provider responses live as JSON files in the fixtures directory
(default: tests/fixtures/llm). Lookup order for a call by (agent, prompt):

  1. ``<fixtures_dir>/<key>.json`` where key = sha256("{agent}::{prompt}")[:16]
     — an exact recorded call (written by scripts/record_fixtures.py, Phase 5)
  2. any fixture whose ``match`` substring(s) all occur in the prompt for that
     agent — scanned in filename order; used to script multi-step behaviour
  3. ``<fixtures_dir>/<agent>.json`` — an agent-level default response

Fixture file format::

    {
      "agent": "supervisor",
      "prompt": "...",                     # informational (exact fixtures)
      "match": ["Create an execution plan", "vector databases"],  # optional
      "response": {
        "text": "...",
        "model": "gpt-4o",
        "prompt_tokens": 123,
        "completion_tokens": 45
      }
    }
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str = "mock"
    prompt_tokens: int = 0
    completion_tokens: int = 0


class FixtureNotFoundError(LookupError):
    pass


def fixture_key(agent: str, prompt: str) -> str:
    return hashlib.sha256(f"{agent}::{prompt}".encode()).hexdigest()[:16]


def _to_response(payload: dict) -> LLMResponse:
    response = payload.get("response", {})
    return LLMResponse(
        text=response.get("text", ""),
        model=response.get("model", "mock"),
        prompt_tokens=int(response.get("prompt_tokens", 0)),
        completion_tokens=int(response.get("completion_tokens", 0)),
    )


class MockLLMClient:
    def __init__(self, fixtures_dir: Path | str):
        self.fixtures_dir = Path(fixtures_dir)

    def complete(
        self, agent: str, prompt: str, *, producer_provider: str | None = None
    ) -> LLMResponse:
        exact = self.fixtures_dir / f"{fixture_key(agent, prompt)}.json"
        if exact.exists():
            return _to_response(json.loads(exact.read_text(encoding="utf-8")))

        for path in sorted(self.fixtures_dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            match = payload.get("match")
            if payload.get("agent") != agent or not match:
                continue
            needles = [match] if isinstance(match, str) else match
            if all(needle in prompt for needle in needles):
                return _to_response(payload)

        default = self.fixtures_dir / f"{agent}.json"
        if default.exists():
            return _to_response(json.loads(default.read_text(encoding="utf-8")))

        raise FixtureNotFoundError(
            f"No LLM fixture for agent={agent!r} (key={fixture_key(agent, prompt)}) in "
            f"{self.fixtures_dir}. Add {fixture_key(agent, prompt)}.json for this exact call, "
            f"a match-fixture, or {agent}.json as an agent default."
        )
