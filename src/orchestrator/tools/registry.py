"""Tool registry: registration, permission checks, rate limits, invocation logging.

Every invocation attempt — including rejected and rate-limited ones — is logged
to the invocation store with inputs, outputs, latency, and status.
"""

from __future__ import annotations

import time

from pydantic import ValidationError

from orchestrator.observability.tracing import child_span, set_attr
from orchestrator.tools.base import (
    InvocationRecord,
    InvocationStore,
    RateLimitExceededError,
    ToolContext,
    ToolError,
    ToolExecutionError,
    ToolPermissionError,
    ToolSpec,
    UnknownToolError,
)


class ToolRegistry:
    def __init__(self, store: InvocationStore):
        self._store = store
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"Tool {spec.name!r} already registered")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise UnknownToolError(f"Unknown tool {name!r}") from None

    def tools_for(self, specialist: str) -> list[ToolSpec]:
        return [s for s in self._specs.values() if specialist in s.owners]

    def is_sensitive_call(self, tool_name: str, arguments: dict) -> bool:
        """Whether this specific invocation would be sensitive (pre-flight check)."""
        try:
            spec = self.get(tool_name)
            return spec.is_sensitive(spec.input_schema.model_validate(arguments))
        except Exception:
            return False  # let invoke() produce the proper error/logging

    def describe_for(self, specialist: str) -> str:
        """Prompt block listing the tools a specialist may call."""
        lines = []
        for spec in self.tools_for(specialist):
            fields = ", ".join(
                f"{name}: {getattr(f.annotation, '__name__', str(f.annotation))}"
                for name, f in spec.input_schema.model_fields.items()
            )
            lines.append(f"- {spec.name}: {spec.description} (arguments: {fields})")
        return "\n".join(lines) if lines else "(no tools available)"

    @staticmethod
    def _span_status(exc: BaseException) -> str:
        if isinstance(exc, (UnknownToolError, ToolPermissionError)):
            return "rejected"
        if isinstance(exc, RateLimitExceededError):
            return "rate_limited"
        return "failure"

    def invoke(self, tool_name: str, arguments: dict, ctx: ToolContext) -> dict:
        """Run a tool for a specialist; returns the output model as a dict."""
        with child_span(
            f"tool:{tool_name}", kind="tool", error_status=self._span_status,
            tool=tool_name, specialist=ctx.specialist, sid=ctx.subtask_id,
        ) as span:
            output = self._invoke_inner(tool_name, arguments, ctx, span)
            return output

    def _invoke_inner(self, tool_name: str, arguments: dict, ctx: ToolContext, span) -> dict:
        log = lambda **kw: self._store.record(  # noqa: E731
            InvocationRecord(
                task_id=ctx.task_id,
                subtask_id=ctx.subtask_id,
                specialist=ctx.specialist,
                tool_name=tool_name,
                arguments=arguments,
                **kw,
            )
        )

        try:
            spec = self.get(tool_name)
        except UnknownToolError as exc:
            log(status="rejected", error=str(exc), latency_ms=0.0)
            raise

        if ctx.specialist not in spec.owners:
            error = f"Specialist {ctx.specialist!r} is not permitted to use {tool_name!r}"
            log(status="rejected", error=error, latency_ms=0.0)
            raise ToolPermissionError(error)

        if self._store.count_executions(ctx.task_id, tool_name) >= spec.rate_limit:
            error = f"Rate limit of {spec.rate_limit} calls per task exceeded for {tool_name!r}"
            log(status="rate_limited", error=error, latency_ms=0.0)
            raise RateLimitExceededError(error)

        try:
            args = spec.input_schema.model_validate(arguments)
        except ValidationError as exc:
            log(status="failure", error=f"Invalid arguments: {exc}", latency_ms=0.0)
            raise ToolExecutionError(f"Invalid arguments for {tool_name!r}: {exc}") from exc

        sensitive = spec.is_sensitive(args)
        started = time.perf_counter()
        try:
            output = spec.handler(args, ctx)
        except Exception as exc:
            latency = (time.perf_counter() - started) * 1000
            log(status="failure", error=str(exc), latency_ms=latency, sensitive=sensitive)
            if isinstance(exc, ToolError):
                raise
            raise ToolExecutionError(f"{tool_name} failed: {exc}") from exc

        latency = (time.perf_counter() - started) * 1000
        output_dict = output.model_dump(mode="json")
        log(status="success", output=output_dict, latency_ms=latency, sensitive=sensitive)
        set_attr(span, "latency_ms", latency)
        set_attr(span, "sensitive", sensitive)
        set_attr(span, "arguments", arguments)
        return output_dict
