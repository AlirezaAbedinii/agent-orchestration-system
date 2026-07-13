"""Task runner: builds the production graph once and executes tasks on it."""

from __future__ import annotations

import logging
from functools import lru_cache

from orchestrator.db.repo import DBInvocationStore, DBLLMCallStore, DBTaskRepo, MemoryEventStore
from orchestrator.graph.builder import build_graph
from orchestrator.graph.checkpointing import get_checkpointer
from orchestrator.hitl.queue import ApprovalQueue
from orchestrator.llm.clients import get_llm_client
from orchestrator.memory.longterm import LongTermMemory
from orchestrator.memory.working import WorkingMemory
from orchestrator.observability.tracing import TracedLLMClient, setup_tracing, task_run_span
from orchestrator.tools.defaults import build_default_registry

logger = logging.getLogger(__name__)


@lru_cache
def get_production_graph():
    setup_tracing()
    return build_graph(
        llm=TracedLLMClient(get_llm_client(), calls=DBLLMCallStore()),
        registry=build_default_registry(DBInvocationStore()),
        repo=DBTaskRepo(),
        checkpointer=get_checkpointer(),
        working=WorkingMemory(),
        longterm=LongTermMemory(),
        memory_events=MemoryEventStore(),
        approvals=ApprovalQueue(),
    )


def run_task(task_id: str) -> None:
    repo = DBTaskRepo()
    bundle = repo.get_task(task_id)
    if bundle is None:
        logger.error("run_task: task %s not found", task_id)
        return
    initial = {
        "task_id": task_id,
        "request": bundle["request"],
        "user_id": bundle["user_id"],
        "require_human_review": bundle["require_human_review"],
        "subtask_results": {},
        "dispatch_log": [],
    }
    # build (and set up tracing) before opening the root span — a root started
    # under the default no-op provider would never be recorded
    graph = get_production_graph()
    try:
        with task_run_span(task_id, "task"):
            graph.invoke(initial, config={"configurable": {"thread_id": task_id}})
    except Exception as error:
        logger.exception("Task %s crashed", task_id)
        repo.set_status(task_id, "failed", error=str(error))


def resume_task(task_id: str, decision: dict) -> None:
    """Resume a paused task from its checkpoint with the human decision."""
    from langgraph.types import Command

    graph = get_production_graph()
    try:
        with task_run_span(task_id, "task:resume"):
            graph.invoke(
                Command(resume=decision), config={"configurable": {"thread_id": task_id}}
            )
    except Exception as error:
        logger.exception("Task %s crashed while resuming", task_id)
        DBTaskRepo().set_status(task_id, "failed", error=str(error))
