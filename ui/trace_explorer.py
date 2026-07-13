"""Trace explorer: span tree, cost dashboards, and replay controls.

Runs against the orchestration API:
    ORCHESTRATOR_API_URL=http://localhost:8080 streamlit run ui/trace_explorer.py --server.port 8512
"""

from __future__ import annotations

import json
import os
from collections import defaultdict

import httpx
import streamlit as st

API_URL = os.environ.get("ORCHESTRATOR_API_URL", "http://localhost:8080")

STATUS_EMOJI = {"success": "🟢", "warning": "🟡", "failure": "🔴", "escalated": "🟠"}
TASK_EMOJI = {
    "completed": "✅", "failed": "❌", "rejected": "🛑",
    "awaiting_approval": "⏸️", "executing": "⚙️", "planning": "📝", "pending": "…",
}


def api(method: str, path: str, **kwargs):
    response = httpx.request(method, f"{API_URL}{path}", timeout=60, **kwargs)
    response.raise_for_status()
    return response.json()


st.set_page_config(page_title="Trace Explorer — Agent Orchestration", page_icon="🔬", layout="wide")
st.title("🔬 Trace explorer")

# ---------------------------------------------------------------- sidebar --
with st.sidebar:
    if st.button("🔄 Refresh", use_container_width=True):
        st.rerun()
    tasks = api("GET", "/traces/tasks", params={"limit": 25})["tasks"]
    if not tasks:
        st.info("No tasks yet.")
        st.stop()
    labels = {}
    for task in tasks:
        emoji = TASK_EMOJI.get(task["status"], "•")
        replay_marker = " ⏪" if task["replay_of"] else ""
        labels[
            f"{emoji} {task['task_id'][:8]}{replay_marker} · ${task['total_usd']:.4f} · {task['request'][:40]}"
        ] = task["task_id"]
    task_id = labels[st.radio("Tasks", list(labels))]

trace_tab, costs_tab, replay_tab = st.tabs(["🌳 Trace", "💰 Costs", "⏪ Replay"])

# ------------------------------------------------------------------ trace --
with trace_tab:
    trace = api("GET", f"/traces/{task_id}")
    spans = trace["spans"]
    calls_by_span = {call["span_id"]: call for call in trace["llm_calls"]}

    if not spans:
        st.info("No spans recorded for this task (it may predate tracing).")
    else:
        children = defaultdict(list)
        for span in spans:
            children[span["parent_id"]].append(span)

        lines: list[str] = []

        def render(span: dict, depth: int) -> None:
            emoji = STATUS_EMOJI.get(span["status"], "⚪")
            cost = span["attributes"].get("cost_usd")
            cost_text = f" · ${cost:.5f}" if cost else ""
            lines.append(
                f"{'&nbsp;' * 4 * depth}{emoji} **{span['name']}** "
                f"`{span['kind']}` · {span['duration_ms']:.0f} ms{cost_text}"
            )
            for child in sorted(children[span["id"]], key=lambda s: s["start_time"]):
                render(child, depth + 1)

        for root in sorted(children[None], key=lambda s: s["start_time"]):
            render(root, 0)
        st.markdown("\n\n".join(lines), unsafe_allow_html=True)

        st.divider()
        st.markdown("#### Inspect a span")
        span_labels = {
            f"{STATUS_EMOJI.get(s['status'], '⚪')} {s['name']} · {s['kind']} · {s['id'][:8]}": s
            for s in spans
        }
        selected = span_labels[st.selectbox("Span", list(span_labels))]
        st.json(
            {
                "status": selected["status"],
                "agent": selected["agent"],
                "duration_ms": selected["duration_ms"],
                "attributes": selected["attributes"],
            }
        )
        call = calls_by_span.get(selected["id"])
        if call:
            st.caption(
                f"model {call['model']} · {call['prompt_tokens']}→{call['completion_tokens']} tokens "
                f"· ${call['cost_usd']:.5f}"
            )
            with st.expander("Full prompt", expanded=False):
                st.code(call["prompt"])
            with st.expander("Full response", expanded=True):
                st.code(call["response"])

# ------------------------------------------------------------------ costs --
with costs_tab:
    costs = api("GET", f"/traces/{task_id}/costs")
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Total cost", f"${costs['total_usd']:.4f}")
    m2.metric(
        "Tokens",
        f"{costs['llm']['total_prompt_tokens'] + costs['llm']['total_completion_tokens']:,}",
    )
    m3.metric("Wall clock", f"{costs['wall_clock_s']:.1f}s")
    m4.metric("Human review", f"{costs['human_review_seconds']:.0f}s")
    m5.metric("Tool calls", costs["total_tool_calls"])

    st.markdown("#### Cost by agent and model")
    st.dataframe(costs["llm"]["by_agent_model"], use_container_width=True)
    if costs["tool_calls"]:
        st.markdown("#### Tool calls")
        st.dataframe(
            [{"tool": tool, **statuses} for tool, statuses in costs["tool_calls"].items()],
            use_container_width=True,
        )

    st.divider()
    st.markdown("### Across all tasks")
    aggregates = api("GET", "/traces/aggregates/costs")
    left, right = st.columns(2)
    with left:
        st.markdown("**Cost per task type**")
        st.dataframe(aggregates["cost_by_task_type"], use_container_width=True)
        st.markdown("**Most expensive agents**")
        st.dataframe(aggregates["most_expensive_agents"], use_container_width=True)
    with right:
        st.markdown("**Tool usage patterns**")
        st.dataframe(
            [{"tool": tool, **stats} for tool, stats in aggregates["tool_usage"].items()],
            use_container_width=True,
        )
        st.markdown("**Escalation rate trend**")
        st.dataframe(aggregates["escalation_trend"], use_container_width=True)

# ----------------------------------------------------------------- replay --
with replay_tab:
    steps = api("GET", f"/replay/{task_id}/steps")["steps"]
    if not steps:
        st.info("No recorded LLM calls to replay.")
    else:
        st.markdown(f"#### Recorded steps ({len(steps)})")
        st.dataframe(
            [
                {
                    "step": i + 1,
                    "agent": s["agent"],
                    "model": s["model"],
                    "response": s["response"],
                }
                for i, s in enumerate(steps)
            ],
            use_container_width=True,
            height=240,
        )

        col_replay, col_fork = st.columns(2)
        with col_replay:
            st.markdown("**Deterministic replay** — recorded outputs, zero API calls.")
            if st.button("▶️ Replay unchanged", use_container_width=True):
                launched = api("POST", f"/replay/{task_id}", json={})
                st.success(f"Replay started: `{launched['replay_task_id']}` — refresh the sidebar.")
        with col_fork:
            st.markdown("**Fork at a step** — edit one response, execution diverges live.")
            step_labels = {
                f"step {i + 1} · {s['agent']} · {s['response'][:60]}": s
                for i, s in enumerate(steps)
            }
            chosen = step_labels[st.selectbox("Modify which step?", list(step_labels))]
            new_response = st.text_area("Replacement response", value=chosen["response"], height=140)
            if st.button("🔱 Fork from this step", use_container_width=True):
                launched = api(
                    "POST",
                    f"/replay/{task_id}",
                    json={"llm_call_id": chosen["id"], "response_text": new_response},
                )
                st.success(f"Fork started: `{launched['replay_task_id']}` — refresh the sidebar.")

        forks = [t for t in tasks if t["replay_of"] == task_id]
        if forks:
            st.divider()
            st.markdown("#### Compare against the original")
            fork_choice = st.selectbox(
                "Replay/fork run", [t["task_id"] for t in forks],
                format_func=lambda tid: f"{tid[:8]} ({next(t['status'] for t in forks if t['task_id'] == tid)})",
            )
            comparison = api("GET", f"/replay/{fork_choice}/compare")
            st.caption(f"{comparison['diverged_steps']} of {len(comparison['steps'])} steps diverged")
            st.dataframe(
                [
                    {
                        "step": s["step"],
                        "": "✳️" if s["diverged"] else "",
                        "agent": (s["original"] or s["fork"])["agent"],
                        "original": (s["original"] or {}).get("response", "—"),
                        "fork": (s["fork"] or {}).get("response", "—"),
                    }
                    for s in comparison["steps"]
                ],
                use_container_width=True,
                height=280,
            )
            left, right = st.columns(2)
            with left:
                st.markdown("**Original final output**")
                st.code(comparison["final_output"]["original"] or "—")
            with right:
                marker = " ✳️" if comparison["final_output"]["diverged"] else ""
                st.markdown(f"**Fork final output{marker}**")
                st.code(comparison["final_output"]["fork"] or "—")
    if task_id and (replay_of := next((t["replay_of"] for t in tasks if t["task_id"] == task_id), None)):
        st.caption(f"This task is itself a replay of `{replay_of}`.")
