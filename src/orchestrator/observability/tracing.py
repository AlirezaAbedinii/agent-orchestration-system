"""Execution tracing: OpenTelemetry spans exported to Postgres.

Every planning decision, specialist step, tool call, reviewer evaluation,
memory retrieval, and escalation becomes a span with custom `orchestrator.*`
attributes, exported synchronously by a custom SpanExporter into the `spans`
table (no collector/Jaeger service needed — the trace explorer reads SQL).
Full LLM prompts/responses are stored in `llm_calls`, referenced by span id.

Tracing is opt-in per process: nothing is exported until `setup_tracing()`
installs the provider (the production runner does; unit tests never do, so
their spans are no-ops). LangGraph runs nodes in worker threads where OTel
context does not propagate, so each task run opens a root span whose context
is kept in a task_id-keyed registry; node spans attach to it explicitly and
everything inside a node (LLM, tool, review, memory spans) nests via normal
context propagation.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import lru_cache

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode

logger = logging.getLogger(__name__)

_TRACER_NAME = "orchestrator"
_current_task_id: ContextVar[str | None] = ContextVar("orchestrator_task_id", default=None)
_task_roots: dict[str, object] = {}  # task_id -> otel Context holding the run's root span


# --- exporter ---------------------------------------------------------------


def _ns_to_dt(nanoseconds: int) -> datetime:
    return datetime.fromtimestamp(nanoseconds / 1e9, tz=timezone.utc)


def span_to_row(span: ReadableSpan) -> dict:
    context = span.get_span_context()
    attributes = dict(span.attributes or {})
    status = attributes.pop("orchestrator.status", None) or (
        "failure" if span.status.status_code is StatusCode.ERROR else "success"
    )
    return {
        "id": format(context.span_id, "016x"),
        "trace_id": format(context.trace_id, "032x"),
        "parent_id": format(span.parent.span_id, "016x") if span.parent else None,
        "task_id": attributes.pop("orchestrator.task_id", None),
        "name": span.name,
        "kind": attributes.pop("orchestrator.kind", "node"),
        "agent": attributes.pop("orchestrator.agent", None),
        "status": status,
        "attributes": attributes,
        "start_time": _ns_to_dt(span.start_time),
        "end_time": _ns_to_dt(span.end_time),
        "duration_ms": (span.end_time - span.start_time) / 1e6,
    }


class PostgresSpanExporter(SpanExporter):
    """Writes finished spans into the `spans` table (or a test sink)."""

    def __init__(self, session_factory=None, sink: Callable[[list[dict]], None] | None = None):
        self._sessions = session_factory
        self._sink = sink

    def export(self, spans) -> SpanExportResult:
        rows = [row for row in (span_to_row(s) for s in spans) if row["task_id"]]
        if not rows:
            return SpanExportResult.SUCCESS
        if self._sink is not None:
            self._sink(rows)
            return SpanExportResult.SUCCESS
        try:
            from orchestrator.db.models import SpanRow
            from orchestrator.db.session import get_sessionmaker

            sessions = self._sessions or get_sessionmaker()
            with sessions() as session, session.begin():
                for row in rows:
                    session.add(SpanRow(**row))
            return SpanExportResult.SUCCESS
        except Exception as error:
            logger.warning("Span export failed: %s", error)
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:  # pragma: no cover - nothing to release
        return None


@lru_cache
def setup_tracing() -> None:
    """Install the global tracer provider with the Postgres exporter (idempotent)."""
    provider = TracerProvider(
        resource=Resource.create({"service.name": "agent-orchestration-system"})
    )
    provider.add_span_processor(SimpleSpanProcessor(PostgresSpanExporter()))
    trace.set_tracer_provider(provider)


# --- span helpers ------------------------------------------------------------


def _tracer():
    return trace.get_tracer(_TRACER_NAME)


def current_task_id() -> str | None:
    return _current_task_id.get()


def set_attr(span, key: str, value) -> None:
    """Attribute setter that coerces non-primitive values to short JSON."""
    if value is None:
        return
    if isinstance(value, (str, bool, int, float)):
        span.set_attribute(key, value)
        return
    try:
        span.set_attribute(key, json.dumps(value, default=str)[:800])
    except Exception:
        span.set_attribute(key, str(value)[:800])


def _mark_exception_status(span, exc: BaseException) -> None:
    try:
        from langgraph.errors import GraphInterrupt

        if isinstance(exc, GraphInterrupt):
            span.set_attribute("orchestrator.status", "escalated")
            return
    except Exception:  # pragma: no cover - langgraph always importable here
        pass
    span.set_attribute("orchestrator.status", "failure")


@contextmanager
def task_run_span(task_id: str, name: str = "task"):
    """Root span for one graph run (initial run, resume, or replay)."""
    token = _current_task_id.set(task_id)
    try:
        with _tracer().start_as_current_span(name) as root:
            root.set_attribute("orchestrator.task_id", task_id)
            root.set_attribute("orchestrator.kind", "task")
            _task_roots[task_id] = trace.set_span_in_context(root)
            try:
                yield root
            except BaseException as exc:
                _mark_exception_status(root, exc)
                raise
            finally:
                _task_roots.pop(task_id, None)
    finally:
        _current_task_id.reset(token)


@contextmanager
def node_span(task_id: str, name: str, kind: str = "node", **attrs):
    """Span for a graph node; attaches to the task's root span across threads."""
    parent_context = _task_roots.get(task_id)
    token = _current_task_id.set(task_id)
    try:
        with _tracer().start_as_current_span(name, context=parent_context) as span:
            span.set_attribute("orchestrator.task_id", task_id)
            span.set_attribute("orchestrator.kind", kind)
            for key, value in attrs.items():
                set_attr(span, key, value)
            try:
                yield span
            except BaseException as exc:
                _mark_exception_status(span, exc)
                raise
    finally:
        _current_task_id.reset(token)


@contextmanager
def child_span(
    name: str,
    kind: str,
    error_status: Callable[[BaseException], str] | None = None,
    **attrs,
):
    """Span nested under whatever is current (LLM/tool/review/memory work)."""
    with _tracer().start_as_current_span(name) as span:
        task_id = current_task_id()
        if task_id:
            span.set_attribute("orchestrator.task_id", task_id)
        span.set_attribute("orchestrator.kind", kind)
        for key, value in attrs.items():
            set_attr(span, key, value)
        try:
            yield span
        except BaseException as exc:
            if error_status is not None:
                span.set_attribute("orchestrator.status", error_status(exc))
            else:
                _mark_exception_status(span, exc)
            raise


# --- LLM wrapper -------------------------------------------------------------


class TracedLLMClient:
    """Wraps any LLM client: every call gets a span and a durable llm_calls row
    with the full prompt/response, token usage, and computed cost."""

    def __init__(self, inner, calls=None):
        self.inner = inner
        self._calls = calls

    def complete(self, agent: str, prompt: str, *, producer_provider: str | None = None):
        from orchestrator.llm.pricing import cost_usd

        with child_span(f"llm:{agent}", kind="llm", agent=agent) as span:
            span.set_attribute("orchestrator.agent", agent)
            response = self.inner.complete(agent, prompt, producer_provider=producer_provider)
            cost = cost_usd(response.model, response.prompt_tokens, response.completion_tokens)
            set_attr(span, "model", response.model)
            set_attr(span, "prompt_tokens", response.prompt_tokens)
            set_attr(span, "completion_tokens", response.completion_tokens)
            set_attr(span, "cost_usd", cost)
            if self._calls is not None:
                span_context = span.get_span_context()
                span_id = (
                    format(span_context.span_id, "016x") if span_context.span_id else None
                )
                self._calls.record(
                    span_id=span_id,
                    task_id=current_task_id(),
                    agent=agent,
                    model=response.model,
                    prompt=prompt,
                    response=response.text,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                    cost_usd=cost,
                )
        return response
