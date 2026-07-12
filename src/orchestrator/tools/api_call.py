"""HTTP API tool with a host allowlist; POST is tagged sensitive."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from orchestrator.config import get_settings
from orchestrator.tools.base import ToolContext, ToolExecutionError, ToolSpec

_TRUNCATE = 5_000


class ApiCallInput(BaseModel):
    method: Literal["GET", "POST"] = "GET"
    url: str = Field(min_length=1)
    json_body: dict | None = None


class ApiCallOutput(BaseModel):
    status_code: int
    body: str


def _check_allowlist(url: str) -> None:
    host = urlparse(url).hostname or ""
    allowlist = get_settings().api_call_allowlist
    if not any(host == entry or host.endswith(f".{entry}") for entry in allowlist):
        raise ToolExecutionError(f"Host {host!r} is not in the API allowlist {allowlist}")


def handle(args: ApiCallInput, ctx: ToolContext) -> ApiCallOutput:
    _check_allowlist(args.url)
    settings = get_settings()
    if settings.mock_llm:
        return ApiCallOutput(status_code=200, body=f"[mock] response from {args.url}")
    response = httpx.request(args.method, args.url, json=args.json_body, timeout=20)
    return ApiCallOutput(status_code=response.status_code, body=response.text[:_TRUNCATE])


SPEC = ToolSpec(
    name="api_call",
    description="Call an allowlisted HTTP API (GET or POST)",
    input_schema=ApiCallInput,
    output_schema=ApiCallOutput,
    owners=frozenset({"research"}),
    rate_limit=10,
    handler=handle,
    sensitive_when=lambda args: args.method == "POST",
)
