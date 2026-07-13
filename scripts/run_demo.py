#!/usr/bin/env python
"""Showcase demo (plan §5 Phase 5.1): the vector-database research scenario.

Drives the composed stack end-to-end over the HTTP API:

  1. seeds long-term memory by running a prior comparison task to completion
     (so the showcase planning step is visibly memory-informed),
  2. submits the showcase task — "research the top 3 open-source vector
     databases, compare their GitHub statistics, analyze the trade-offs,
     produce a recommendation memo" — with require_human_review=True,
  3. auto-approves the plan gate and leaves the FINAL deliverable approval to
     you in the review UI (pass --auto-approve to run fully unattended),
  4. prints the run as it happens (parallel research fan-out, the reviewer
     rejecting the citation-free memo draft, the rework), then the final memo,
     memory influence, escalations, cost, and links to both UIs.

Usage:
    docker compose up -d --build      # or: make infra + make dev (inline mode)
    python scripts/run_demo.py [--auto-approve]

Only stdlib + httpx are used; the script runs from the host against the API.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

WARMUP_REQUEST = (
    "Compare open-source vector databases: gather facts about Chroma from the web, "
    "compute the GitHub star ranking from the demo database, generate a comparison "
    "table using Python, and write a comparison memo saved as memo.md."
)
DEMO_REQUEST = (
    "Research the top 3 open-source vector databases (Chroma, Qdrant, Weaviate), "
    "extract and compare their GitHub statistics, analyze the trade-offs, and "
    "produce a one-page recommendation memo with cited sources."
)

TERMINAL = {"completed", "failed", "rejected"}


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def die(message: str) -> None:
    print(f"\nDEMO FAILED: {message}", file=sys.stderr)
    sys.exit(1)


class Demo:
    def __init__(self, args: argparse.Namespace):
        self.api = httpx.Client(base_url=args.api, timeout=30)
        self.args = args

    # ------------------------------------------------------------- polling --

    def submit(self, request: str, *, require_human_review: bool = False) -> str:
        response = self.api.post(
            "/tasks",
            json={
                "request": request,
                "user_id": self.args.user,
                "require_human_review": require_human_review,
            },
        )
        response.raise_for_status()
        task_id = response.json()["task_id"]
        print(f"task submitted: {task_id}")
        return task_id

    def pending_approvals(self, task_id: str) -> list[dict]:
        rows = self.api.get("/approvals", params={"status": "pending"}).json()["approvals"]
        return [row for row in rows if row["task_id"] == task_id]

    def resolve(self, approval: dict, notes: str) -> None:
        self.api.post(
            f"/approvals/{approval['id']}/resolve", json={"action": "approve", "notes": notes}
        ).raise_for_status()

    def watch(self, task_id: str, *, plan_gate_note: str | None = None) -> dict:
        """Poll the task, narrating status/subtask transitions and handling
        approval gates until the task reaches a terminal state."""
        seen_status = None
        seen_subtasks: dict[str, tuple] = {}
        final_gate_announced = False
        deadline = time.monotonic() + self.args.timeout

        while time.monotonic() < deadline:
            bundle = self.api.get(f"/tasks/{task_id}").json()

            if bundle["status"] != seen_status:
                seen_status = bundle["status"]
                print(f"status → {seen_status}")

            for subtask in bundle.get("subtasks") or []:
                key = (subtask["status"], subtask["review_score"])
                if seen_subtasks.get(subtask["sid"]) == key:
                    continue
                seen_subtasks[subtask["sid"]] = key
                score = f" (review {subtask['review_score']}/5)" if subtask["review_score"] else ""
                note = ""
                if subtask["status"] == "rework":
                    note = f" — {(subtask.get('review_feedback') or '').split('.')[0]}"
                print(f"  [{subtask['sid']:>3} {subtask['specialist']:<8}] {subtask['status']}{score}{note}")

            if bundle["status"] in TERMINAL:
                return bundle

            if bundle["status"] == "awaiting_approval":
                for approval in self.pending_approvals(task_id):
                    if approval["gate_key"] == "plan":
                        print(
                            f"\n⏸ plan gate: trigger={approval['trigger']} level={approval['level']}"
                            f"\n  plan confidence: {bundle.get('confidence')}"
                            f"\n  {plan_gate_note or 'approving plan.'}"
                        )
                        self.resolve(approval, "demo: plan gate auto-approved")
                    elif self.args.auto_approve:
                        print(f"\n⏸ {approval['gate_key']} gate → auto-approving (--auto-approve)")
                        self.resolve(approval, "demo: auto-approved")
                    elif not final_gate_announced:
                        final_gate_announced = True
                        print(
                            f"\n⏸ HUMAN APPROVAL NEEDED — the final memo is waiting for you."
                            f"\n  Open the review UI:  {self.args.review_ui}"
                            f"\n  Approval {approval['id']} ({approval['level']}, {approval['trigger']})"
                            f"\n  Click Approve to deliver the memo. Waiting…"
                        )

            time.sleep(self.args.poll_interval)

        die(f"timed out after {self.args.timeout}s waiting for task {task_id}")

    # ------------------------------------------------------------ epilogue --

    def print_trace_story(self, task_id: str) -> None:
        trace = self.api.get(f"/traces/{task_id}").json()
        spans = trace["spans"]

        waves = []
        for span in spans:
            if span["name"] != "schedule":
                continue
            wave = span["attributes"].get("wave")
            if isinstance(wave, str):  # list attributes are stored JSON-encoded
                try:
                    wave = json.loads(wave)
                except ValueError:
                    continue
            if wave:
                waves.append(wave)
        if waves:
            print("execution waves (parallel fan-out within a wave):")
            for index, wave in enumerate(waves, 1):
                marker = "  ⇉ parallel fan-out" if len(wave) > 1 else ""
                print(f"  wave {index}: {', '.join(wave)}{marker}")

        reviews = [span for span in spans if span["kind"] == "review"]
        by_sid: dict[str, list] = {}
        for span in sorted(reviews, key=lambda s: s["start_time"]):
            by_sid.setdefault(span["attributes"]["sid"], []).append(span["attributes"])
        print("reviewer verdicts:")
        for sid, attrs in sorted(by_sid.items()):
            scores = " → ".join(str(a.get("score")) for a in attrs)
            print(f"  {sid}: {scores}/5")
            for attrs_entry in attrs:
                if (attrs_entry.get("score") or 5) < 3:
                    print(f"      rejected: {attrs_entry.get('feedback')}")

        for span in spans:
            if span["name"] == "memory:retrieve":
                count = span["attributes"].get("retrieved_count", 0)
                print(f"memory retrieval at planning: {count} memories injected")
            if span["name"] == "memory:extract":
                print(f"memory write-back: {span['attributes'].get('stored_count', 0)} new memories stored")

        approvals = self.api.get("/approvals", params={"task_id": task_id}).json()["approvals"]
        print("escalations:")
        for row in sorted(approvals, key=lambda r: r["created_at"]):
            seconds = f"{row['review_seconds']:.1f}s" if row["review_seconds"] is not None else "—"
            print(
                f"  {row['gate_key']}: {row['trigger']} → {row['level']} "
                f"→ {row['resolution_action'] or row['status']} (review time {seconds})"
            )

        costs = self.api.get(f"/traces/{task_id}/costs").json()
        llm = costs["llm"]
        print(
            f"cost: ${costs['total_usd']} — {llm['total_calls']} LLM calls, "
            f"{llm['total_prompt_tokens']}+{llm['total_completion_tokens']} tokens, "
            f"{costs['total_tool_calls']} tool calls, "
            f"wall clock {costs['wall_clock_s']:.1f}s, "
            f"human review {costs['human_review_seconds']:.1f}s"
        )

    # ----------------------------------------------------------------- run --

    def run(self) -> None:
        try:
            health = self.api.get("/health").json()
        except httpx.HTTPError as error:
            die(f"API not reachable at {self.args.api} ({error}). Run `docker compose up -d --build` first.")
        section(f"Agent orchestration demo — API {self.args.api} (run_mode={health['run_mode']})")

        if self.args.skip_warmup:
            print("skipping the warm-up task (--skip-warmup); planning may retrieve no memories")
        else:
            section("Step 1/3 — seed long-term memory with a prior comparison task")
            warmup_id = self.submit(WARMUP_REQUEST)
            warmup = self.watch(warmup_id)
            if warmup["status"] != "completed":
                die(f"warm-up task ended {warmup['status']}: {warmup.get('error')}")
            memories = self.api.get(f"/memory/users/{self.args.user}").json()
            total = sum(memories.get("counts", {}).values())
            print(f"long-term memory now holds {total} records for user '{self.args.user}'")

        section("Step 2/3 — showcase task (user-requested human review)")
        print(f"request: {DEMO_REQUEST}\n")
        task_id = self.submit(DEMO_REQUEST, require_human_review=True)
        bundle = self.watch(
            task_id,
            plan_gate_note=(
                "demo auto-approves the PLAN gate — the final deliverable approval is yours."
                if not self.args.auto_approve
                else "auto-approving (--auto-approve)."
            ),
        )
        if bundle["status"] != "completed":
            die(f"showcase task ended {bundle['status']}: {bundle.get('error')}")

        section("Step 3/3 — deliverable")
        print(bundle["final_output"])

        section("What just happened (from the execution trace)")
        self.print_trace_story(task_id)

        section("Explore the run")
        print(
            f"review UI       {self.args.review_ui}\n"
            f"trace explorer  {self.args.trace_ui}  (task {task_id}: span tree, prompts, costs, replay)"
        )


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)  # narration streams even when piped
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8080")
    parser.add_argument("--review-ui", default="http://localhost:8511")
    parser.add_argument("--trace-ui", default="http://localhost:8512")
    parser.add_argument("--user", default="default")
    parser.add_argument("--auto-approve", action="store_true",
                        help="resolve every gate programmatically (unattended run)")
    parser.add_argument("--skip-warmup", action="store_true",
                        help="skip the memory-seeding warm-up task")
    parser.add_argument("--timeout", type=float, default=600.0, help="per-task timeout in seconds")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    Demo(parser.parse_args()).run()


if __name__ == "__main__":
    main()
