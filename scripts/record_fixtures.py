#!/usr/bin/env python
"""Capture live LLM responses as MOCK_LLM fixtures (plan §7 determinism strategy).

Runs a request through the full graph against the real providers, recording
every (agent, prompt) → response pair as ``<out>/<sha16>.json`` — the
exact-match form the fixture player checks first (see orchestrator/llm/mock.py).
Escalations are auto-approved so recording runs unattended.

Requires real API keys (OPENAI_API_KEY, ANTHROPIC_API_KEY) and the infra
services from ``make infra`` (Postgres for task rows and tool logging; Redis
and Chroma are used when reachable, silently downgraded otherwise). Refuses to
run under MOCK_LLM=1 — recording playback output would be circular.

Usage:
    python scripts/record_fixtures.py "Compare X and Y and write a memo" \
        --out tests/fixtures/llm/recorded

Play the recording back:
    MOCK_LLM=1 LLM_FIXTURES_DIR=tests/fixtures/llm/recorded make dev
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


class RecordingLLM:
    """Wraps the real client and writes one exact-key fixture per call."""

    def __init__(self, inner, out_dir: Path):
        from orchestrator.llm.mock import fixture_key

        self._inner = inner
        self._out = out_dir
        self._key = fixture_key
        self.recorded = 0

    def complete(self, agent: str, prompt: str, *, producer_provider: str | None = None):
        response = self._inner.complete(agent, prompt, producer_provider=producer_provider)
        payload = {
            "agent": agent,
            "prompt": prompt,
            "response": {
                "text": response.text,
                "model": response.model,
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
            },
        }
        path = self._out / f"{self._key(agent, prompt)}.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.recorded += 1
        print(f"  recorded {agent:<10} → {path.name}")
        return response


def _optional_memory_backends():
    """Real Redis/Chroma when reachable; graceful in-memory/None fallback."""
    from orchestrator.memory.working import InMemoryWorkingMemory, WorkingMemory

    try:
        working = WorkingMemory()
        working._redis.ping()
    except Exception as error:
        print(f"note: Redis unreachable ({error}); using in-memory working memory")
        working = InMemoryWorkingMemory()

    try:
        from orchestrator.memory.longterm import LongTermMemory

        longterm = LongTermMemory()
        longterm.all_items("facts")
    except Exception as error:
        print(f"note: Chroma unreachable ({error}); recording without long-term memory")
        longterm = None
    return working, longterm


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("request", help="task request to run and record")
    parser.add_argument("--user", default="default")
    parser.add_argument("--out", type=Path, default=Path("tests/fixtures/llm/recorded"))
    parser.add_argument("--require-human-review", action="store_true",
                        help="also exercise (and auto-approve) the plan/final gates")
    args = parser.parse_args()

    from orchestrator.config import get_settings

    settings = get_settings()
    if settings.mock_llm:
        sys.exit("MOCK_LLM=1 is set — recording the fixture player is circular. Unset it and retry.")
    if not (settings.openai_api_key and settings.anthropic_api_key):
        sys.exit("OPENAI_API_KEY and ANTHROPIC_API_KEY are required to record live responses.")

    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    from orchestrator.db.repo import DBInvocationStore, DBTaskRepo, MemoryEventStore
    from orchestrator.graph.builder import build_graph
    from orchestrator.hitl.queue import InMemoryApprovalQueue
    from orchestrator.llm.clients import RealLLMClient
    from orchestrator.tools.defaults import build_default_registry

    args.out.mkdir(parents=True, exist_ok=True)
    recorder = RecordingLLM(RealLLMClient(), args.out)
    working, longterm = _optional_memory_backends()

    repo = DBTaskRepo()
    graph = build_graph(
        llm=recorder,
        registry=build_default_registry(DBInvocationStore()),
        repo=repo,
        checkpointer=MemorySaver(),
        working=working,
        longterm=longterm,
        memory_events=MemoryEventStore() if longterm is not None else None,
        approvals=InMemoryApprovalQueue(),
    )

    task_id = repo.create_task(
        request=args.request, user_id=args.user, require_human_review=args.require_human_review
    )
    config = {"configurable": {"thread_id": task_id}}
    print(f"recording task {task_id}: {args.request}")
    graph.invoke(
        {
            "task_id": task_id,
            "request": args.request,
            "user_id": args.user,
            "require_human_review": args.require_human_review,
            "subtask_results": {},
            "dispatch_log": [],
        },
        config=config,
    )
    while graph.get_state(config).next:  # auto-approve any escalation and resume
        print("  escalation hit → auto-approving to keep the recording unattended")
        graph.invoke(Command(resume={"action": "approve", "notes": "record_fixtures auto-approval"}), config=config)

    bundle = repo.get_task(task_id) or {}
    print(
        f"\ndone: task {bundle.get('status')}, {recorder.recorded} fixtures written to {args.out}\n"
        f"play back with: MOCK_LLM=1 LLM_FIXTURES_DIR={args.out} pytest / make dev"
    )


if __name__ == "__main__":
    main()
