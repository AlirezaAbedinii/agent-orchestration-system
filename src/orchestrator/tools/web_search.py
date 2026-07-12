"""Web search tool: Tavily API with DuckDuckGo fallback; canned under MOCK_LLM.

External effects are mocked at the tool boundary (plan §7): with MOCK_LLM=1 the
handler returns deterministic results without any network call.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from orchestrator.config import get_settings
from orchestrator.tools.base import ToolContext, ToolSpec


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str


class WebSearchInput(BaseModel):
    query: str = Field(min_length=1)
    max_results: int = Field(default=5, ge=1, le=10)


class WebSearchOutput(BaseModel):
    results: list[SearchResult]


def _tavily(query: str, max_results: int, api_key: str) -> list[SearchResult]:
    response = httpx.post(
        "https://api.tavily.com/search",
        json={"api_key": api_key, "query": query, "max_results": max_results},
        timeout=15,
    )
    response.raise_for_status()
    return [
        SearchResult(title=r.get("title", ""), url=r.get("url", ""), snippet=r.get("content", ""))
        for r in response.json().get("results", [])
    ]


def _duckduckgo(query: str, max_results: int) -> list[SearchResult]:
    response = httpx.get(
        "https://api.duckduckgo.com/",
        params={"q": query, "format": "json", "no_html": 1},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    results = []
    if payload.get("AbstractText"):
        results.append(
            SearchResult(
                title=payload.get("Heading", query),
                url=payload.get("AbstractURL", ""),
                snippet=payload["AbstractText"],
            )
        )
    for topic in payload.get("RelatedTopics", []):
        if "Text" in topic and len(results) < max_results:
            results.append(
                SearchResult(title=topic["Text"][:80], url=topic.get("FirstURL", ""), snippet=topic["Text"])
            )
    return results[:max_results]


def handle(args: WebSearchInput, ctx: ToolContext) -> WebSearchOutput:
    settings = get_settings()
    if settings.mock_llm:
        return WebSearchOutput(
            results=[
                SearchResult(
                    title=f"[mock] result for {args.query}",
                    url="https://example.com/mock",
                    snippet=f"[mock] result for {args.query}",
                )
            ]
        )
    if settings.tavily_api_key:
        return WebSearchOutput(results=_tavily(args.query, args.max_results, settings.tavily_api_key))
    return WebSearchOutput(results=_duckduckgo(args.query, args.max_results))


SPEC = ToolSpec(
    name="web_search",
    description="Search the web and return result snippets",
    input_schema=WebSearchInput,
    output_schema=WebSearchOutput,
    owners=frozenset({"research"}),
    rate_limit=10,
    handler=handle,
)
