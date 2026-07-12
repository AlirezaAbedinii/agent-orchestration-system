"""Default tool registry wiring (all five Phase 1 tools)."""

from __future__ import annotations

from orchestrator.tools import api_call, code_exec, db_query, file_io, web_search
from orchestrator.tools.base import InvocationStore
from orchestrator.tools.registry import ToolRegistry


def build_default_registry(store: InvocationStore) -> ToolRegistry:
    registry = ToolRegistry(store)
    for spec in (
        web_search.SPEC,
        file_io.READ_SPEC,
        file_io.WRITE_SPEC,
        code_exec.SPEC,
        db_query.SPEC,
        api_call.SPEC,
    ):
        registry.register(spec)
    return registry
