"""Cost & performance tracking (plan §5 Phase 4.3).

Per task: tokens by agent/model, tool call counts, wall-clock time, human
review time, total dollars. Across tasks, the four rollups: cost per task
type (the task's specialist mix), most expensive agents, tool usage patterns,
and the escalation-rate trend. Everything is computed from the durable
tables (llm_calls, tool_invocations, approvals, subtasks) — no separate
costs table to keep in sync.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

import sqlalchemy as sa

from orchestrator.db.models import ApprovalRow, LLMCallRow, SubtaskRow, Task, ToolInvocation
from orchestrator.db.session import get_sessionmaker


def task_costs(task_id: str, session_factory=None) -> dict | None:
    sessions = session_factory or get_sessionmaker()
    with sessions() as session:
        task = session.get(Task, task_id)
        if task is None:
            return None

        llm_rows = session.execute(
            sa.select(
                LLMCallRow.agent,
                LLMCallRow.model,
                sa.func.count(),
                sa.func.coalesce(sa.func.sum(LLMCallRow.prompt_tokens), 0),
                sa.func.coalesce(sa.func.sum(LLMCallRow.completion_tokens), 0),
                sa.func.coalesce(sa.func.sum(LLMCallRow.cost_usd), 0.0),
            )
            .where(LLMCallRow.task_id == task_id)
            .group_by(LLMCallRow.agent, LLMCallRow.model)
        ).all()
        by_agent_model = [
            {
                "agent": agent,
                "model": model,
                "calls": calls,
                "prompt_tokens": int(prompt_tokens),
                "completion_tokens": int(completion_tokens),
                "cost_usd": float(cost),
            }
            for agent, model, calls, prompt_tokens, completion_tokens, cost in llm_rows
        ]

        tool_rows = session.execute(
            sa.select(ToolInvocation.tool_name, ToolInvocation.status, sa.func.count())
            .where(ToolInvocation.task_id == task_id)
            .group_by(ToolInvocation.tool_name, ToolInvocation.status)
        ).all()
        tool_calls: dict[str, dict[str, int]] = defaultdict(dict)
        for tool_name, status, count in tool_rows:
            tool_calls[tool_name][status] = count

        review_seconds, escalations = session.execute(
            sa.select(
                sa.func.coalesce(sa.func.sum(ApprovalRow.review_seconds), 0.0),
                sa.func.count(),
            ).where(ApprovalRow.task_id == task_id)
        ).one()

        end = task.completed_at or datetime.now(timezone.utc)
        return {
            "task_id": task_id,
            "status": task.status,
            "llm": {
                "by_agent_model": sorted(by_agent_model, key=lambda r: -r["cost_usd"]),
                "total_calls": sum(r["calls"] for r in by_agent_model),
                "total_prompt_tokens": sum(r["prompt_tokens"] for r in by_agent_model),
                "total_completion_tokens": sum(r["completion_tokens"] for r in by_agent_model),
            },
            "tool_calls": dict(tool_calls),
            "total_tool_calls": int(sum(sum(v.values()) for v in tool_calls.values())),
            "wall_clock_s": max(0.0, (end - task.created_at).total_seconds()),
            "human_review_seconds": float(review_seconds),
            "escalations": int(escalations),
            "total_usd": round(sum(r["cost_usd"] for r in by_agent_model), 8),
        }


def aggregate_costs(session_factory=None) -> dict:
    sessions = session_factory or get_sessionmaker()
    with sessions() as session:
        cost_per_task = {
            task_id: float(cost)
            for task_id, cost in session.execute(
                sa.select(LLMCallRow.task_id, sa.func.sum(LLMCallRow.cost_usd))
                .where(LLMCallRow.task_id.is_not(None))
                .group_by(LLMCallRow.task_id)
            )
        }

        # task type = the set of specialists its plan used
        mixes: dict[str, set] = defaultdict(set)
        for task_id, specialist in session.execute(
            sa.select(SubtaskRow.task_id, SubtaskRow.specialist).distinct()
        ):
            mixes[task_id].add(specialist)

        tasks = session.execute(sa.select(Task.id, sa.func.date(Task.created_at))).all()
        escalated_tasks = set(session.scalars(sa.select(ApprovalRow.task_id).distinct()))

        by_type: dict[str, dict] = defaultdict(lambda: {"tasks": 0, "total_usd": 0.0})
        for task_id, _ in tasks:
            task_type = "+".join(sorted(mixes[task_id])) if mixes.get(task_id) else "unplanned"
            by_type[task_type]["tasks"] += 1
            by_type[task_type]["total_usd"] += cost_per_task.get(task_id, 0.0)
        cost_by_task_type = [
            {
                "task_type": task_type,
                "tasks": stats["tasks"],
                "total_usd": round(stats["total_usd"], 8),
                "avg_usd": round(stats["total_usd"] / stats["tasks"], 8),
            }
            for task_type, stats in sorted(by_type.items(), key=lambda kv: -kv[1]["total_usd"])
        ]

        agent_rows = session.execute(
            sa.select(
                LLMCallRow.agent,
                sa.func.count(),
                sa.func.coalesce(sa.func.sum(LLMCallRow.prompt_tokens + LLMCallRow.completion_tokens), 0),
                sa.func.coalesce(sa.func.sum(LLMCallRow.cost_usd), 0.0),
            ).group_by(LLMCallRow.agent)
        ).all()
        most_expensive_agents = sorted(
            (
                {"agent": agent, "calls": calls, "tokens": int(tokens), "total_usd": float(cost)}
                for agent, calls, tokens, cost in agent_rows
            ),
            key=lambda r: -r["total_usd"],
        )

        tool_rows = session.execute(
            sa.select(ToolInvocation.tool_name, ToolInvocation.status, sa.func.count())
            .group_by(ToolInvocation.tool_name, ToolInvocation.status)
        ).all()
        tool_usage: dict[str, dict] = defaultdict(lambda: {"total": 0})
        for tool_name, status, count in tool_rows:
            tool_usage[tool_name][status] = count
            tool_usage[tool_name]["total"] += count

        by_day: dict[str, dict] = defaultdict(lambda: {"tasks": 0, "escalated": 0})
        for task_id, day in tasks:
            key = day.isoformat()
            by_day[key]["tasks"] += 1
            if task_id in escalated_tasks:
                by_day[key]["escalated"] += 1
        escalation_trend = [
            {
                "date": day,
                "tasks": stats["tasks"],
                "escalated": stats["escalated"],
                "rate": round(stats["escalated"] / stats["tasks"], 3),
            }
            for day, stats in sorted(by_day.items())
        ]

        return {
            "cost_by_task_type": cost_by_task_type,
            "most_expensive_agents": most_expensive_agents,
            "tool_usage": dict(tool_usage),
            "escalation_trend": escalation_trend,
        }
