"""LangGraph state machine (plan §5 Phase 1.4 + Phase 3 human-in-the-loop).

intake → plan → (plan gate) → schedule ⇄ execute/gather loops → synthesize
→ (final gate) → deliver, with conditional edges for rework and
retry-with-revised-approach, and four human-in-the-loop decision points:

  escalate_plan     — low plan confidence / user-requested review
  escalate_subtask  — double specialist failure / review score exhausted
  tool gate         — sensitive tool call inside a specialist's loop
  final_gate        — user-requested approval of the final deliverable

Escalations pause the run via `interrupt()` (state checkpointed to Postgres)
after enqueueing a durable approval with the full decision context; resolution
resumes the graph with the human decision. Note that an interrupted node
re-executes its pre-interrupt code on resume — everything before an interrupt
is therefore idempotent (the approval queue is keyed by task_id + gate_key).

Dependencies (LLM client, tool registry, repo, memory, approval queue,
checkpointer) are injectable so unit tests can run the full graph without
Postgres or provider APIs.
"""

from __future__ import annotations

import logging

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from orchestrator.agents.reviewer import Reviewer
from orchestrator.agents.specialists import make_specialists
from orchestrator.agents.supervisor import Supervisor
from orchestrator.config import get_settings
from orchestrator.graph.edges import (
    after_gather,
    compute_wave,
    dispatch,
    plan_gate,
    route_after_final_gate,
    route_after_plan_escalation,
    route_after_subtask_escalation,
)
from orchestrator.graph.state import TaskState
from orchestrator.hitl.levels import ApprovalLevel, level_for
from orchestrator.hitl.triggers import Trigger, plan_escalation, sensitive_operation, subtask_escalation
from orchestrator.llm.clients import LLMClient, get_llm_client
from orchestrator.llm.router import route
from orchestrator.memory.extraction import extract_memories, store_extracted
from orchestrator.memory.retrieval import retrieve_for_planning
from orchestrator.observability.tracing import child_span, node_span, set_attr
from orchestrator.planning.decomposer import PlanValidationError
from orchestrator.planning.schemas import ExecutionPlan
from orchestrator.tools.base import ToolContext
from orchestrator.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def build_graph(
    llm: LLMClient | None = None,
    registry: ToolRegistry | None = None,
    repo=None,
    checkpointer=None,
    working=None,
    longterm=None,
    memory_events=None,
    approvals=None,
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
    if working is None:
        from orchestrator.memory.working import InMemoryWorkingMemory

        working = InMemoryWorkingMemory()
    if approvals is None:
        from orchestrator.hitl.queue import ApprovalQueue

        approvals = ApprovalQueue()

    supervisor = Supervisor(llm)
    reviewer = Reviewer(llm)
    specialists = make_specialists(llm, registry)

    # --- helpers -----------------------------------------------------------

    def _relevant_memories(request: str, user_id: str) -> list[dict]:
        if longterm is None:
            return []
        try:
            hits = []
            for kind in ("episodes", "facts", "preferences"):
                hits.extend(longterm.query(kind, request, user_id=user_id, k=2))
            return [{"id": h.id, "kind": h.kind, "text": h.text} for h in hits]
        except Exception as error:
            logger.warning("Memory lookup for approval context failed: %s", error)
            return []

    def _package(state: TaskState, *, current_step: dict, proposed_action: dict | None, reasoning: str) -> dict:
        """Full decision context pushed to the review queue."""
        results = state.get("subtask_results", {})
        user_id = state.get("user_id", "default")
        return {
            "task": {
                "task_id": state["task_id"],
                "request": state["request"],
                "user_id": user_id,
            },
            "plan": state.get("plan"),
            "completed_steps": {
                sid: r.get("output") for sid, r in results.items() if r.get("status") == "completed"
            },
            "subtask_states": {
                sid: {k: r.get(k) for k in ("status", "attempts", "error_count", "rework_count", "score", "error")}
                for sid, r in results.items()
            },
            "current_step": current_step,
            "proposed_action": proposed_action,
            "reasoning": reasoning,
            "relevant_memories": _relevant_memories(state["request"], user_id),
        }

    def _decision_parts(decision) -> tuple[str, dict, str]:
        decision = decision or {}
        return (
            decision.get("action", "approve"),
            decision.get("payload") or {},
            decision.get("notes", ""),
        )

    # --- nodes -------------------------------------------------------------

    def intake(state: TaskState) -> dict:
        with node_span(state["task_id"], "intake"):
            repo.set_status(state["task_id"], "planning")
            working.start(state["task_id"], state.get("user_id", "default"))
            return {}

    def plan_node(state: TaskState) -> dict:
        with node_span(state["task_id"], "plan", kind="planning") as span:
            memories_block = None
            retrieved_ids: list[str] = []
            if longterm is not None:
                try:
                    retrieved = retrieve_for_planning(
                        longterm,
                        user_id=state.get("user_id", "default"),
                        request=state["request"],
                        task_id=state["task_id"],
                        events=memory_events,
                    )
                except Exception as error:  # memory must never block planning
                    logger.warning("Memory retrieval failed for %s: %s", state["task_id"], error)
                    retrieved = None
                if retrieved is not None:
                    memories_block, retrieved_ids = retrieved.block, retrieved.ids
            set_attr(span, "memories_injected", len(retrieved_ids))
            try:
                plan = supervisor.plan(state["request"], memories=memories_block)
            except PlanValidationError as error:
                span.set_attribute("orchestrator.status", "failure")
                repo.set_status(state["task_id"], "failed", error=str(error))
                return {"plan": None, "plan_error": str(error)}
            repo.save_plan(state["task_id"], plan)
            working.set_plan(state["task_id"], plan.model_dump())
            set_attr(span, "confidence", plan.confidence)
            set_attr(span, "subtask_count", len(plan.subtasks))
            return {
                "plan": plan.model_dump(),
                "confidence": plan.confidence,
                "retrieved_memory_ids": retrieved_ids,
            }

    def escalate_plan(state: TaskState) -> dict:
        escalation = plan_escalation(
            state.get("confidence", 1.0),
            state.get("require_human_review", False),
            settings.plan_confidence_threshold,
        )
        level = level_for(escalation.trigger)
        with node_span(
            state["task_id"], "escalate:plan", kind="escalation",
            trigger=escalation.trigger.value, level=level.value,
        ) as espan:
            return _escalate_plan_body(state, escalation, level, espan)

    def _escalate_plan_body(state: TaskState, escalation, level, espan) -> dict:
        proposed = {"type": "execute_plan", "plan": state.get("plan")}
        package = _package(
            state, current_step={"gate": "plan"}, proposed_action=proposed, reasoning=escalation.reason
        )
        common = dict(
            task_id=state["task_id"],
            gate_key="plan",
            trigger=escalation.trigger.value,
            context=package,
            proposed_action=proposed,
            reasoning=escalation.reason,
        )
        if level is ApprovalLevel.NOTIFY:
            approvals.record_notify(**common)
            return {"hitl_decision": {"gate": "plan", "action": "notified"}}

        repo.set_status(state["task_id"], "awaiting_approval")
        approval_id, _ = approvals.ensure(level=level.value, **common)
        set_attr(espan, "approval_id", approval_id)
        decision = interrupt(
            {"approval_id": approval_id, "trigger": escalation.trigger.value, "level": level.value, **package}
        )
        action, payload, notes = _decision_parts(decision)
        set_attr(espan, "resolution", action)

        if action == "reject":
            repo.set_status(state["task_id"], "rejected", error=f"Plan rejected by reviewer: {notes}")
            return {"hitl_decision": {"gate": "plan", "action": "reject", "notes": notes}}
        if action == "take_over":
            repo.set_status(state["task_id"], "executing")
            return {
                "hitl_decision": {"gate": "plan", "action": "take_over"},
                "final_output": payload.get("final_output", ""),
            }
        if action == "modify":
            new_plan = ExecutionPlan.model_validate(payload["plan"])
            repo.save_plan(state["task_id"], new_plan)
            working.set_plan(state["task_id"], new_plan.model_dump())
            repo.set_status(state["task_id"], "executing")
            return {
                "hitl_decision": {"gate": "plan", "action": "modify"},
                "plan": new_plan.model_dump(),
                "confidence": new_plan.confidence,
            }
        repo.set_status(state["task_id"], "executing")
        return {"hitl_decision": {"gate": "plan", "action": "approve"}}

    def schedule(state: TaskState) -> dict:
        with node_span(state["task_id"], "schedule") as span:
            repo.set_status(state["task_id"], "executing")
            wave = compute_wave(state)
            set_attr(span, "wave", [payload["spec"]["id"] for payload in wave])
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

        def tool_gate(tool_name: str, arguments: dict, iteration: int, transcript: list[str]) -> dict:
            escalation = sensitive_operation(tool_name, True)
            level = level_for(escalation.trigger)
            proposed = {
                "type": "tool_call",
                "tool": tool_name,
                "arguments": arguments,
                "sid": sid,
                "specialist": spec["specialist"],
            }
            bundle = repo.get_task(task_id) or {}
            package = {
                "task": {
                    "task_id": task_id,
                    "request": bundle.get("request", ""),
                    "user_id": bundle.get("user_id", "default"),
                },
                "plan": bundle.get("plan"),
                "completed_steps": {
                    s["sid"]: s.get("output")
                    for s in bundle.get("subtasks", [])
                    if s.get("status") == "completed"
                },
                "current_step": {
                    "gate": "tool",
                    "sid": sid,
                    "description": spec["description"],
                    "iteration": iteration,
                    "transcript_tail": transcript[-3:],
                },
                "proposed_action": proposed,
                "reasoning": escalation.reason,
                "relevant_memories": _relevant_memories(
                    bundle.get("request", ""), bundle.get("user_id", "default")
                ),
            }
            common = dict(
                task_id=task_id,
                gate_key=f"tool:{sid}:{tool_name}:{iteration}",
                trigger=escalation.trigger.value,
                context=package,
                proposed_action=proposed,
                reasoning=escalation.reason,
            )
            with child_span(
                f"approval:tool:{tool_name}", kind="escalation",
                trigger=escalation.trigger.value, level=level.value, tool=tool_name,
            ) as espan:
                if level is ApprovalLevel.NOTIFY:
                    approvals.record_notify(**common)
                    set_attr(espan, "resolution", "notified")
                    return {"action": "approve"}
                repo.set_status(task_id, "awaiting_approval")
                approval_id, _ = approvals.ensure(level=level.value, **common)
                set_attr(espan, "approval_id", approval_id)
                decision = interrupt(
                    {"approval_id": approval_id, "trigger": escalation.trigger.value, "level": level.value, **package}
                )
                repo.set_status(task_id, "executing")
                set_attr(espan, "resolution", (decision or {}).get("action", "approve"))
                return decision or {"action": "approve"}

        with node_span(
            task_id, f"execute:{sid}", kind="specialist",
            sid=sid, specialist=spec["specialist"], attempt=attempts,
        ) as span:
            return _execute_body(payload, spec, sid, task_id, base, attempts, ctx, tool_gate, span)

    def _execute_body(payload, spec, sid, task_id, base, attempts, ctx, tool_gate, span) -> dict:
        repo.record_subtask(task_id, sid, status="running", attempts=attempts)
        try:
            result = specialists[spec["specialist"]].execute(
                spec, payload.get("inputs", {}), payload.get("feedback"), ctx, gate=tool_gate
            )
            with child_span(f"review:{sid}", kind="review", sid=sid) as review_span:
                verdict = reviewer.review(
                    spec["description"],
                    spec.get("expected_output_format", "plain text"),
                    result.output,
                    producer_provider=route(spec["specialist"]).provider,
                )
                set_attr(review_span, "score", verdict.score)
                set_attr(review_span, "feedback", verdict.feedback[:300])
        except GraphInterrupt:
            # the sensitive-tool gate paused the run — not a specialist failure
            raise
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
            working.record_error(task_id, sid, str(error))
            span.set_attribute("orchestrator.status", "failure")
            set_attr(span, "error", str(error)[:300])
            return {"subtask_results": {sid: entry}}

        set_attr(span, "tool_calls", result.tool_calls)
        set_attr(span, "review_score", verdict.score)
        approved = verdict.score >= settings.review_score_threshold
        if not approved:
            span.set_attribute("orchestrator.status", "warning")
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
            working.record_subtask_output(task_id, sid, result.output)
            working.set_intermediate(task_id, f"tools:{sid}", result.tool_calls)
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
        with node_span(state["task_id"], "gather") as span:
            for sid in sorted(state.get("subtask_results", {})):
                result = state["subtask_results"][sid]
                escalation = subtask_escalation(
                    sid,
                    result,
                    review_threshold=settings.review_score_threshold,
                    max_retries=settings.max_specialist_retries,
                )
                if escalation:
                    span.set_attribute("orchestrator.status", "warning")
                    set_attr(span, "escalating", escalation.trigger.value)
                    return {
                        "needs_escalation": True,
                        "escalation": {"trigger": escalation.trigger.value, "reason": escalation.reason, "sid": sid},
                        "escalation_reason": escalation.reason,
                    }
            return {"needs_escalation": False, "escalation": None}

    def escalate_subtask(state: TaskState) -> dict:
        info = state["escalation"]
        sid = info["sid"]
        trigger = Trigger(info["trigger"])
        level = level_for(trigger)
        with node_span(
            state["task_id"], f"escalate:subtask:{sid}", kind="escalation",
            trigger=trigger.value, level=level.value, sid=sid,
        ) as espan:
            return _escalate_subtask_body(state, info, sid, trigger, level, espan)

    def _escalate_subtask_body(state: TaskState, info, sid, trigger, level, espan) -> dict:
        result = state["subtask_results"][sid]
        spec = next(s for s in state["plan"]["subtasks"] if s["id"] == sid)
        suggested = result.get("feedback") or "Try a different approach."
        proposed = {
            "type": "retry_subtask",
            "sid": sid,
            "specialist": spec["specialist"],
            "feedback": suggested,
        }
        package = _package(
            state,
            current_step={
                "gate": "subtask",
                "sid": sid,
                "description": spec["description"],
                "attempts": result.get("attempts", 0),
            },
            proposed_action=proposed,
            reasoning=info["reason"],
        )
        common = dict(
            task_id=state["task_id"],
            gate_key=f"subtask:{sid}:a{result.get('attempts', 0)}",
            trigger=trigger.value,
            context=package,
            proposed_action=proposed,
            reasoning=info["reason"],
        )
        retry_entry = {**result, "status": "rework", "feedback": suggested, "error_count": 0, "rework_count": 0}
        if level is ApprovalLevel.NOTIFY:
            approvals.record_notify(**common)
            return {
                "hitl_decision": {"gate": "subtask", "action": "notified"},
                "needs_escalation": False,
                "subtask_results": {sid: retry_entry},
            }

        repo.set_status(state["task_id"], "awaiting_approval")
        approval_id, _ = approvals.ensure(level=level.value, **common)
        set_attr(espan, "approval_id", approval_id)
        decision = interrupt(
            {"approval_id": approval_id, "trigger": trigger.value, "level": level.value, **package}
        )
        action, payload, notes = _decision_parts(decision)
        set_attr(espan, "resolution", action)

        if action == "reject":
            repo.set_status(
                state["task_id"], "rejected", error=f"Subtask {sid} rejected by reviewer: {notes}"
            )
            return {"hitl_decision": {"gate": "subtask", "action": "reject"}, "needs_escalation": False}
        if action == "take_over":
            output = payload.get("output", "")
            repo.record_subtask(
                state["task_id"], sid,
                status="completed", output=output, review_feedback=f"human take-over: {notes}",
            )
            working.record_subtask_output(state["task_id"], sid, output)
            repo.set_status(state["task_id"], "executing")
            return {
                "hitl_decision": {"gate": "subtask", "action": "take_over"},
                "needs_escalation": False,
                "subtask_results": {
                    sid: {**result, "status": "completed", "output": output, "human_provided": True}
                },
            }
        feedback = payload.get("feedback", suggested) if action == "modify" else (notes or suggested)
        repo.set_status(state["task_id"], "executing")
        return {
            "hitl_decision": {"gate": "subtask", "action": action},
            "needs_escalation": False,
            "subtask_results": {sid: {**retry_entry, "feedback": feedback}},
        }

    def synthesize(state: TaskState) -> dict:
        with node_span(state["task_id"], "synthesize", kind="synthesis") as span:
            outputs = {
                sid: result.get("output", "")
                for sid, result in state.get("subtask_results", {}).items()
                if result.get("status") == "completed"
            }
            set_attr(span, "inputs", sorted(outputs))
            return {"final_output": supervisor.synthesize(state["request"], outputs)}

    def final_gate(state: TaskState) -> dict:
        if not state.get("require_human_review"):
            return {}
        with node_span(
            state["task_id"], "escalate:final", kind="escalation",
            trigger=Trigger.USER_REQUESTED.value, level=ApprovalLevel.APPROVE_ACTION.value,
        ) as espan:
            proposed = {"type": "deliver", "final_output": state.get("final_output")}
            reasoning = "User requested review of the final deliverable"
            package = _package(
                state, current_step={"gate": "final"}, proposed_action=proposed, reasoning=reasoning
            )
            repo.set_status(state["task_id"], "awaiting_approval")
            approval_id, _ = approvals.ensure(
                task_id=state["task_id"],
                gate_key="final",
                trigger=Trigger.USER_REQUESTED.value,
                level=ApprovalLevel.APPROVE_ACTION.value,
                context=package,
                proposed_action=proposed,
                reasoning=reasoning,
            )
            set_attr(espan, "approval_id", approval_id)
            decision = interrupt(
                {
                    "approval_id": approval_id,
                    "trigger": Trigger.USER_REQUESTED.value,
                    "level": ApprovalLevel.APPROVE_ACTION.value,
                    **package,
                }
            )
            action, payload, notes = _decision_parts(decision)
            set_attr(espan, "resolution", action)
            if action == "reject":
                repo.set_status(
                    state["task_id"], "rejected", error=f"Final deliverable rejected by reviewer: {notes}"
                )
                return {"hitl_decision": {"gate": "final", "action": "reject"}}
            if action in ("modify", "take_over"):
                return {
                    "hitl_decision": {"gate": "final", "action": action},
                    "final_output": payload.get("final_output", state.get("final_output")),
                }
            return {"hitl_decision": {"gate": "final", "action": "approve"}}

    def deliver(state: TaskState) -> dict:
        with node_span(state["task_id"], "deliver"):
            repo.set_final_output(state["task_id"], state.get("final_output") or "")
            if longterm is not None:
                try:
                    with child_span("memory:extract", kind="memory") as memory_span:
                        results = state.get("subtask_results", {})
                        extracted = extract_memories(
                            llm,
                            request=state["request"],
                            outputs={
                                sid: r.get("output", "")
                                for sid, r in results.items()
                                if r.get("status") == "completed"
                            },
                            tools_used={sid: r.get("tool_calls", []) for sid, r in results.items()},
                            final_output=state.get("final_output") or "",
                        )
                        stored = store_extracted(
                            longterm,
                            extracted,
                            user_id=state.get("user_id", "default"),
                            task_id=state["task_id"],
                            events=memory_events,
                        )
                        set_attr(memory_span, "stored_count", len(stored))
                        set_attr(memory_span, "memory_ids", [mid for _, mid in stored])
                except Exception as error:  # memory must never fail a delivered task
                    logger.warning("Memory extraction failed for %s: %s", state["task_id"], error)
            # Working memory is scoped to a single task: cleared on completion.
            # Escalated tasks keep theirs — they resume from the checkpoint.
            working.clear(state["task_id"])
            return {}

    # --- graph -------------------------------------------------------------

    graph = StateGraph(TaskState)
    graph.add_node("intake", intake)
    graph.add_node("plan", plan_node)
    graph.add_node("escalate_plan", escalate_plan)
    graph.add_node("schedule", schedule)
    graph.add_node("execute", execute)
    graph.add_node("gather", gather)
    graph.add_node("escalate_subtask", escalate_subtask)
    graph.add_node("synthesize", synthesize)
    graph.add_node("final_gate", final_gate)
    graph.add_node("deliver", deliver)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "plan")
    graph.add_conditional_edges(
        "plan", plan_gate, {"schedule": "schedule", "escalate_plan": "escalate_plan", "failed": END}
    )
    graph.add_conditional_edges(
        "escalate_plan",
        route_after_plan_escalation,
        {"schedule": "schedule", "deliver": "deliver", "end": END},
    )
    graph.add_conditional_edges("schedule", dispatch, ["execute", "synthesize"])
    graph.add_edge("execute", "gather")
    graph.add_conditional_edges(
        "gather", after_gather, {"escalate": "escalate_subtask", "schedule": "schedule"}
    )
    graph.add_conditional_edges(
        "escalate_subtask", route_after_subtask_escalation, {"schedule": "schedule", "end": END}
    )
    graph.add_edge("synthesize", "final_gate")
    graph.add_conditional_edges(
        "final_gate", route_after_final_gate, {"deliver": "deliver", "end": END}
    )
    graph.add_edge("deliver", END)

    return graph.compile(checkpointer=checkpointer)
