"""LLM client abstraction: real provider clients or the fixture player.

Agents call ``client.complete(agent, prompt, producer_provider=...)`` and stay
oblivious to providers; routing happens here (via llm/router.py). MOCK_LLM=1
swaps in the fixture player so nothing leaves the process.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Protocol

from orchestrator.config import get_settings
from orchestrator.llm.mock import LLMResponse, MockLLMClient
from orchestrator.llm.router import route


class LLMClient(Protocol):
    def complete(
        self, agent: str, prompt: str, *, producer_provider: str | None = None
    ) -> LLMResponse: ...


class RealLLMClient:
    """Routes each call to OpenAI or Anthropic via the langchain chat models."""

    @staticmethod
    @lru_cache(maxsize=8)
    def _chat_model(provider: str, model: str):
        settings = get_settings()
        if provider == "openai":
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(model=model, api_key=settings.openai_api_key, temperature=0)
        if provider == "anthropic":
            from langchain_anthropic import ChatAnthropic

            return ChatAnthropic(model=model, api_key=settings.anthropic_api_key, temperature=0)
        raise ValueError(f"Unknown provider {provider!r}")

    def complete(
        self, agent: str, prompt: str, *, producer_provider: str | None = None
    ) -> LLMResponse:
        choice = route(agent, producer_provider)
        message = self._chat_model(choice.provider, choice.model).invoke(prompt)
        usage = message.usage_metadata or {}
        return LLMResponse(
            text=message.text() if callable(getattr(message, "text", None)) else str(message.content),
            model=f"{choice.provider}:{choice.model}",
            prompt_tokens=int(usage.get("input_tokens", 0)),
            completion_tokens=int(usage.get("output_tokens", 0)),
        )


def get_llm_client() -> LLMClient:
    settings = get_settings()
    if settings.mock_llm:
        return MockLLMClient(settings.llm_fixtures_dir)
    return RealLLMClient()
