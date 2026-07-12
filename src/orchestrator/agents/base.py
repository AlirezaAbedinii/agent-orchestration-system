"""Base agent: named LLM caller with typed structured-output helpers."""

from __future__ import annotations

from orchestrator.llm.clients import LLMClient
from orchestrator.llm.mock import LLMResponse


class BaseAgent:
    name: str = "agent"

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def complete(self, prompt: str, *, producer_provider: str | None = None) -> LLMResponse:
        return self.llm.complete(self.name, prompt, producer_provider=producer_provider)
