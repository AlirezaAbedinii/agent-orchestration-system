"""Fixture-playback LLM client, active when MOCK_LLM=1.

Recorded provider responses live as JSON files in the fixtures directory
(default: tests/fixtures/llm). Lookup order for a call by (agent, prompt):

  1. ``<fixtures_dir>/<key>.json`` where key = sha256("{agent}::{prompt}")[:16]
     — an exact recorded call (written by scripts/record_fixtures.py, Phase 5)
  2. ``<fixtures_dir>/<agent>.json`` — an agent-level default response

Fixture file format::

    {
      "agent": "supervisor",
      "prompt": "...",                     # informational
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


class MockLLMClient:
    def __init__(self, fixtures_dir: Path | str):
        self.fixtures_dir = Path(fixtures_dir)

    def complete(self, agent: str, prompt: str) -> LLMResponse:
        key = fixture_key(agent, prompt)
        for candidate in (self.fixtures_dir / f"{key}.json", self.fixtures_dir / f"{agent}.json"):
            if candidate.exists():
                payload = json.loads(candidate.read_text(encoding="utf-8"))
                response = payload.get("response", {})
                return LLMResponse(
                    text=response.get("text", ""),
                    model=response.get("model", "mock"),
                    prompt_tokens=int(response.get("prompt_tokens", 0)),
                    completion_tokens=int(response.get("completion_tokens", 0)),
                )
        raise FixtureNotFoundError(
            f"No LLM fixture for agent={agent!r} (key={key}) in {self.fixtures_dir}. "
            f"Add {key}.json for this exact call or {agent}.json as an agent default."
        )
