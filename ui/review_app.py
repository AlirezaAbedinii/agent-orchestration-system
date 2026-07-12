"""Human review UI: approval queue, decision context, resolution actions, and
a clarifying-question chat grounded in the paused task's state.

Runs against the orchestration API:
    ORCHESTRATOR_API_URL=http://localhost:8080 streamlit run ui/review_app.py --server.port 8511
"""

from __future__ import annotations

import json
import os

import httpx
import streamlit as st

API_URL = os.environ.get("ORCHESTRATOR_API_URL", "http://localhost:8080")

TRIGGER_LABELS = {
    "low_plan_confidence": "Low plan confidence",
    "specialist_double_failure": "Specialist failed twice",
    "sensitive_operation": "Sensitive operation",
    "low_review_score": "Review score too low",
    "user_requested": "User-requested review",
}
LEVEL_BADGES = {
    "approve_plan": "🗺️ Approve plan",
    "approve_action": "⚡ Approve action",
    "take_over": "🧑‍💻 Take over",
    "notify": "🔔 Notify",
}


def api(method: str, path: str, **kwargs):
    response = httpx.request(method, f"{API_URL}{path}", timeout=30, **kwargs)
    response.raise_for_status()
    return response.json()


def default_modify_payload(approval: dict) -> dict:
    gate = approval["gate_key"]
    proposed = approval.get("proposed_action") or {}
    if gate == "plan":
        return {"plan": (approval.get("context") or {}).get("plan")}
    if gate.startswith("tool:"):
        return {"arguments": proposed.get("arguments", {})}
    if gate == "final":
        return {"final_output": proposed.get("final_output", "")}
    return {"feedback": proposed.get("feedback", "")}


def take_over_payload(approval: dict, text: str) -> dict:
    gate = approval["gate_key"]
    if gate in ("plan", "final"):
        return {"final_output": text}
    return {"output": text}


def resolve(approval: dict, action: str, payload: dict | None, notes: str) -> None:
    try:
        api(
            "POST",
            f"/approvals/{approval['id']}/resolve",
            json={"action": action, "payload": payload, "notes": notes},
        )
        st.session_state.pop(f"chat:{approval['id']}", None)
        st.success(f"Resolved: {action}. The task is resuming.")
        st.rerun()
    except httpx.HTTPStatusError as error:
        st.error(f"Resolution failed: {error.response.text}")


st.set_page_config(page_title="Review Queue — Agent Orchestration", page_icon="🛎️", layout="wide")
st.title("🛎️ Human review queue")

# ---------------------------------------------------------------- sidebar --
with st.sidebar:
    if st.button("🔄 Refresh queue", use_container_width=True):
        st.rerun()
    pending = api("GET", "/approvals", params={"status": "pending"})["approvals"]
    st.caption(f"{len(pending)} pending approval(s)")
    selected_id = None
    if pending:
        labels = {
            f"{TRIGGER_LABELS.get(a['trigger'], a['trigger'])} · task {a['task_id'][:8]}": a["id"]
            for a in pending
        }
        choice = st.radio("Pending decisions", list(labels))
        selected_id = labels[choice]
    resolved_rows = api("GET", "/approvals", params={"status": "resolved"})["approvals"][:5]
    if resolved_rows:
        st.divider()
        st.caption("Recently resolved")
        for row in resolved_rows:
            st.caption(
                f"· {row['resolution_action']} — {TRIGGER_LABELS.get(row['trigger'], row['trigger'])} "
                f"({row['review_seconds']:.0f}s review)" if row["review_seconds"] is not None
                else f"· {row['resolution_action']} — {row['trigger']}"
            )

if not pending:
    st.info("Nothing waiting for review. Escalations will appear here.")
    st.stop()

approval = api("GET", f"/approvals/{selected_id}")
context = approval.get("context") or {}
task = context.get("task", {})

# ------------------------------------------------------------------ header --
badge = LEVEL_BADGES.get(approval["level"], approval["level"])
st.subheader(f"{badge} — {TRIGGER_LABELS.get(approval['trigger'], approval['trigger'])}")
st.markdown(
    f"**Task** `{approval['task_id']}` · **gate** `{approval['gate_key']}` · "
    f"**queued at** {approval['created_at']}"
)
st.markdown(f"**Why escalated:** {approval.get('reasoning') or '—'}")

left, right = st.columns([3, 2], gap="large")

with left:
    st.markdown("#### Task context")
    st.markdown(f"> {task.get('request', '—')}")

    plan = context.get("plan") or {}
    subtask_states = context.get("subtask_states") or {}
    if plan.get("subtasks"):
        st.markdown("#### Execution progress")
        st.table(
            [
                {
                    "subtask": s["id"],
                    "specialist": s["specialist"],
                    "description": s["description"][:80],
                    "status": (subtask_states.get(s["id"]) or {}).get("status", "pending"),
                    "attempts": (subtask_states.get(s["id"]) or {}).get("attempts", 0),
                }
                for s in plan["subtasks"]
            ]
        )

    st.markdown("#### Decision point")
    st.json(context.get("current_step", {}))

    st.markdown("#### Proposed action & agent reasoning")
    st.json(approval.get("proposed_action") or {})
    completed = context.get("completed_steps") or {}
    if completed:
        with st.expander(f"Completed step outputs ({len(completed)})"):
            for sid, output in completed.items():
                st.markdown(f"**{sid}**: {output}")

    memories = context.get("relevant_memories") or []
    st.markdown("#### Relevant memories")
    if memories:
        for memory in memories:
            st.markdown(f"- *({memory['kind']})* {memory['text']}")
    else:
        st.caption("No related long-term memories.")

    similar = [
        row
        for row in resolved_rows
        if row["trigger"] == approval["trigger"] and row["id"] != approval["id"]
    ]
    st.markdown("#### Similar past decisions")
    if similar:
        for row in similar[:3]:
            st.markdown(
                f"- task `{row['task_id'][:8]}` → **{row['resolution_action']}**"
                + (f" — “{row['resolution_notes']}”" if row.get("resolution_notes") else "")
            )
    else:
        st.caption("No previous decisions for this trigger.")

with right:
    st.markdown("#### Your decision")
    notes = st.text_input("Notes (recorded with the decision)", key=f"notes:{approval['id']}")

    approve_col, reject_col = st.columns(2)
    with approve_col:
        if st.button("✅ Approve", type="primary", use_container_width=True):
            resolve(approval, "approve", None, notes)
    with reject_col:
        if st.button("🛑 Reject", use_container_width=True):
            resolve(approval, "reject", None, notes)

    with st.expander("✏️ Modify the proposed action"):
        edited = st.text_area(
            "Payload (JSON)",
            value=json.dumps(default_modify_payload(approval), indent=2),
            height=220,
            key=f"modify:{approval['id']}",
        )
        if st.button("Submit modification", use_container_width=True):
            try:
                resolve(approval, "modify", json.loads(edited), notes)
            except json.JSONDecodeError as error:
                st.error(f"Payload is not valid JSON: {error}")

    with st.expander("🧑‍💻 Take over (agents stand down)"):
        human_output = st.text_area(
            "Provide the output yourself", height=180, key=f"takeover:{approval['id']}"
        )
        if st.button("Submit take-over", use_container_width=True):
            if human_output.strip():
                resolve(approval, "take_over", take_over_payload(approval, human_output), notes)
            else:
                st.error("Provide the output text first.")

    st.divider()
    st.markdown("#### Ask the agent")
    st.caption("Clarifying questions answered from the paused task's checkpointed state.")
    chat_key = f"chat:{approval['id']}"
    history = st.session_state.setdefault(chat_key, [])
    for entry in history:
        with st.chat_message(entry["role"]):
            st.markdown(entry["text"])
    question = st.chat_input("e.g. Why is this step needed?")
    if question:
        history.append({"role": "user", "text": question})
        try:
            answer = api(
                "POST", f"/approvals/{approval['id']}/chat", json={"question": question}
            )["answer"]
        except httpx.HTTPStatusError as error:
            answer = f"(chat failed: {error.response.status_code})"
        history.append({"role": "assistant", "text": answer})
        st.rerun()
