"""LangGraph state machine (plan §5 Phase 1.4).

intake → plan → (gate) → schedule ⇄ execute/gather loops → synthesize → deliver
with conditional edges for rework, retry-with-revised-approach, and an
escalation stub (real HITL arrives in Phase 3).

Dependencies (LLM client, tool registry, repo, checkpointer) are injectable so
unit tests can run the full graph without Postgres or provider APIs.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from orchestrator.agents.reviewer import Reviewer
from orchestrator.agents.specialists import make_specialists
from orchestrator.agents.supervisor import Supervisor
from orchestrator.config import get_settings
from orchestrator.graph.edges import after_gather, compute_wave, dispatch, plan_gate
from orchestrator.graph.state import TaskState
from orchestrator.llm.clients import LLMClient, get_llm_client
from orchestrator.llm.router import route
from orchestrator.planning.decomposer import PlanValidationError
from orchestrator.tools.base import ToolContext
from orchestrator.tools.registry import ToolRegistry


def build_graph(
    llm: LLMClient | None = None,
    registry: ToolRegistry | None = None,
    repo=None,
    checkpointer=None,
):
    settings = get_settings()
    llm = llm or get_llm_client()
    if registry is None:
        from orchestrator.db.repo import DBInvocationStore
        from orchestrator.tools.defaults import build_default_registry

        registry = build_default_registry(DBInvocationStore())
    if repo is None:
        from orchestrator.db.repo import DBTaskRepo

        repo = DBTaskRepo()

    supervisor = Supervisor(llm)
    reviewer = Reviewer(llm)
    specialists = make_specialists(llm, registry)

    def intake(state: TaskState) -> dict:
        repo.set_status(state["task_id"], "planning")
        return {}

    def plan_node(state: TaskState) -> dict:
        try:
            plan = supervisor.plan(state["request"])
        except PlanValidationError as error:
            repo.set_status(state["task_id"], "failed", error=str(error))
            return {"plan": None, "plan_error": str(error)}
        repo.save_plan(state["task_id"], plan)
        return {"plan": plan.model_dump(), "confidence": plan.confidence}

    def schedule(state: TaskState) -> dict:
        repo.set_status(state["task_id"], "executing")
        wave = compute_wave(state)
        update: dict = {"current_wave": wave}
        if wave:
            update["dispatch_log"] = [[payload["spec"]["id"] for payload in wave]]
        return update

    def execute(payload: dict) -> dict:
        spec = payload["spec"]
        sid = spec["id"]
        task_id = payload["task_id"]
        prior = payload.get("prior") or {}
        attempts = prior.get("attempts", 0) + 1
        base = {
            "sid": sid,
            "attempts": attempts,
            "error_count": prior.get("error_count", 0),
            "rework_count": prior.get("rework_count", 0),
        }
        ctx = ToolContext(
            task_id=task_id,
            specialist=spec["specialist"],
            subtask_id=sid,
            workspace=settings.workspace_root / task_id,
        )
        repo.record_subtask(task_id, sid, status="running", attempts=attempts)
        try:
            result = specialists[spec["specialist"]].execute(
                spec, payload.get("inputs", {}), payload.get("feedback"), ctx
            )
            verdict = reviewer.review(
                spec["description"],
                spec.get("expected_output_format", "plain text"),
                result.output,
                producer_provider=route(spec["specialist"]).provider,
            )
        except Exception as error:  # failed attempt → retry with revised approach
            entry = {
                **base,
                "status": "failed_attempt",
                "output": None,
                "error": str(error),
                "error_count": base["error_count"] + 1,
                "feedback": f"Previous attempt raised an error: {error}. Try a different approach.",
            }
            repo.record_subtask(task_id, sid, status="failed", attempts=attempts, error=str(error))
            return {"subtask_results": {sid: entry}}

        approved = verdict.score >= settings.review_score_threshold
        entry = {
            **base,
            "output": result.output,
            "score": verdict.score,
            "feedback": verdict.feedback,
            "tool_calls": result.tool_calls,
        }
        if approved:
            entry["status"] = "completed"
            repo.record_subtask(
                task_id, sid,
                status="completed", attempts=attempts, output=result.output,
                review_score=verdict.score, review_feedback=verdict.feedback,
            )
        else:
            entry["status"] = "rework"
            entry["rework_count"] = base["rework_count"] + 1
            repo.record_subtask(
                task_id, sid,
                status="rework", attempts=attempts, output=result.output,
                review_score=verdict.score, review_feedback=verdict.feedback,
            )
        return {"subtask_results": {sid: entry}}

    def gather(state: TaskState) -> dict:
        for sid, result in state.get("subtask_results", {}).items():
            if result.get("status") == "failed_attempt" and result.get("error_count", 0) >= 2:
                return {
                    "needs_escalation": True,
                    "escalation_reason": f"Subtask {sid} failed twice: {result.get('error')}",
                }
            if (
                result.get("status") == "rework"
                and result.get("rework_count", 0) >= settings.max_specialist_retries
            ):
                return {
                    "needs_escalation": True,
                    "escalation_reason": (
                        f"Subtask {sid} review score {result.get('score')} below threshold "
                        f"after {result['rework_count']} rework cycles"
                    ),
                }
        return {"needs_escalation": False}

    def synthesize(state: TaskState) -> dict:
        outputs = {
            sid: result.get("output", "")
            for sid, result in state.get("subtask_results", {}).items()
            if result.get("status") == "completed"
        }
        return {"final_output": supervisor.synthesize(state["request"], outputs)}

    def deliver(state: TaskState) -> dict:
        repo.set_final_output(state["task_id"], state.get("final_output") or "")
        return {}

    def escalate(state: TaskState) -> dict:
        # Phase 1 stub: mark and stop. Phase 3 replaces this with a LangGraph
        # interrupt + approval queue.
        reason = state.get("escalation_reason") or (
            f"Plan confidence {state.get('confidence')} below threshold "
            f"{settings.plan_confidence_threshold}"
        )
        repo.set_status(state["task_id"], "escalated", error=reason)
        return {"needs_escalation": True, "escalation_reason": reason}

    graph = StateGraph(TaskState)
    graph.add_node("intake", intake)
    graph.add_node("plan", plan_node)
    graph.add_node("schedule", schedule)
    graph.add_node("execute", execute)
    graph.add_node("gather", gather)
    graph.add_node("synthesize", synthesize)
    graph.add_node("deliver", deliver)
    graph.add_node("escalate", escalate)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "plan")
    graph.add_conditional_edges(
        "plan", plan_gate, {"schedule": "schedule", "escalate": "escalate", "failed": END}
    )
    graph.add_conditional_edges("schedule", dispatch, ["execute", "synthesize"])
    graph.add_edge("execute", "gather")
    graph.add_conditional_edges(
        "gather", after_gather, {"escalate": "escalate", "schedule": "schedule"}
    )
    graph.add_edge("synthesize", "deliver")
    graph.add_edge("deliver", END)
    graph.add_edge("escalate", END)

    return graph.compile(checkpointer=checkpointer)
