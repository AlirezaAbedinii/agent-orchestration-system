"""Replay system (plan §5 Phase 4.4).

Every LLM call is recorded (llm_calls) with its full prompt and response, so
any past execution can be re-run deterministically: the replay client serves
recorded responses — matched by exact (agent, prompt) first, then per-agent
order for prompts that drifted — and never touches a provider, so a strict
replay costs zero API calls (there is no fallback to fall through to).

A fork modifies one step: recorded calls strictly before step k replay as-is,
step k's response is replaced with the human-provided text, and everything
after runs live (the regular client), so the execution genuinely diverges.
Replays and forks run as new tasks (tasks.replay_of points at the original)
with long-term memory disabled — a replay must not learn.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict, deque

from orchestrator.llm.mock import LLMResponse

logger = logging.getLogger(__name__)


class ReplayDivergenceError(RuntimeError):
    pass


def _prompt_key(agent: str, prompt: str) -> tuple[str, str]:
    return agent, hashlib.sha256(prompt.encode()).hexdigest()


class ReplayLLMClient:
    """Serves recorded LLM responses instead of calling a provider."""

    def __init__(
        self,
        records: list[dict],
        overrides: dict[str, str] | None = None,
        fallback=None,
    ):
        self._records = records
        self._overrides = overrides or {}
        self._fallback = fallback
        self._consumed: set[int] = set()
        self._by_key: dict[tuple[str, str], deque[int]] = defaultdict(deque)
        self._by_agent: dict[str, deque[int]] = defaultdict(deque)
        for index, record in enumerate(records):
            self._by_key[_prompt_key(record["agent"], record["prompt"])].append(index)
            self._by_agent[record["agent"]].append(index)

    def _pop(self, queue: deque[int]) -> int | None:
        while queue:
            index = queue.popleft()
            if index not in self._consumed:
                return index
        return None

    def complete(self, agent: str, prompt: str, *, producer_provider: str | None = None):
        index = self._pop(self._by_key[_prompt_key(agent, prompt)])
        if index is None:
            index = self._pop(self._by_agent[agent])
        if index is None:
            if self._fallback is not None:
                return self._fallback.complete(agent, prompt, producer_provider=producer_provider)
            raise ReplayDivergenceError(
                f"Replay diverged: no recorded call left for agent {agent!r}"
            )
        self._consumed.add(index)
        record = self._records[index]
        if record["id"] in self._overrides:
            return LLMResponse(text=self._overrides[record["id"]], model="replay:modified")
        return LLMResponse(text=record["response"], model=f"replay:{record['model']}")


def create_replay_task(original_task_id: str, llm_call_id: str | None = None) -> tuple[str, str]:
    from orchestrator.db.repo import DBTaskRepo

    repo = DBTaskRepo()
    original = repo.get_task(original_task_id)
    if original is None:
        raise KeyError(f"Task {original_task_id} not found")
    mode = "fork" if llm_call_id else "replay"
    new_task_id = repo.create_task(
        request=original["request"],
        user_id=original["user_id"],
        require_human_review=False,
        replay_of=original_task_id,
    )
    return new_task_id, mode


def run_replay(
    new_task_id: str,
    original_task_id: str,
    llm_call_id: str | None = None,
    response_text: str | None = None,
) -> None:
    from orchestrator.db.repo import DBInvocationStore, DBLLMCallStore, DBTaskRepo
    from orchestrator.graph.builder import build_graph
    from orchestrator.graph.checkpointing import get_checkpointer
    from orchestrator.hitl.queue import ApprovalQueue
    from orchestrator.llm.clients import get_llm_client
    from orchestrator.memory.working import WorkingMemory
    from orchestrator.observability.tracing import TracedLLMClient, task_run_span
    from orchestrator.tools.defaults import build_default_registry

    repo = DBTaskRepo()
    try:
        records = DBLLMCallStore().for_task(original_task_id)
        if llm_call_id is not None:
            k = next((i for i, r in enumerate(records) if r["id"] == llm_call_id), None)
            if k is None:
                raise KeyError(f"LLM call {llm_call_id} not found in task {original_task_id}")
            pool = records[: k + 1]  # strict prefix + the modified step itself
            overrides = {llm_call_id: response_text or ""}
            fallback = get_llm_client()  # everything after step k runs live
            run_name = "task:fork"
        else:
            pool, overrides, fallback, run_name = records, {}, None, "task:replay"

        llm = TracedLLMClient(
            ReplayLLMClient(pool, overrides, fallback), calls=DBLLMCallStore()
        )
        graph = build_graph(
            llm=llm,
            registry=build_default_registry(DBInvocationStore()),
            repo=repo,
            checkpointer=get_checkpointer(),
            working=WorkingMemory(),
            longterm=None,  # replays must not read or write long-term memory
            memory_events=None,
            approvals=ApprovalQueue(),
        )
        bundle = repo.get_task(new_task_id)
        with task_run_span(new_task_id, run_name):
            graph.invoke(
                {
                    "task_id": new_task_id,
                    "request": bundle["request"],
                    "user_id": bundle["user_id"],
                    "require_human_review": False,
                    "subtask_results": {},
                    "dispatch_log": [],
                },
                config={"configurable": {"thread_id": new_task_id}},
            )
    except Exception as error:
        logger.exception("Replay %s of %s crashed", new_task_id, original_task_id)
        repo.set_status(new_task_id, "failed", error=str(error))


def _align_steps(original_calls: list[dict], fork_calls: list[dict]) -> list[dict]:
    """Pair calls by (agent, prompt); leftovers pair by order and are diverged.

    Index-based pairing would mis-align parallel branches whose completion
    order differs between runs; prompt-based pairing keeps identical steps
    matched regardless of thread timing.
    """
    fork_by_key: dict[tuple[str, str], deque[int]] = defaultdict(deque)
    for index, call in enumerate(fork_calls):
        fork_by_key[_prompt_key(call["agent"], call["prompt"])].append(index)

    paired_fork: set[int] = set()
    pairs: list[tuple[dict, dict | None]] = []
    for call in original_calls:
        queue = fork_by_key[_prompt_key(call["agent"], call["prompt"])]
        match = queue.popleft() if queue else None
        if match is not None:
            paired_fork.add(match)
            pairs.append((call, fork_calls[match]))
        else:
            pairs.append((call, None))
    unpaired_fork = [call for i, call in enumerate(fork_calls) if i not in paired_fork]

    def _side(call: dict | None) -> dict | None:
        if call is None:
            return None
        return {
            "id": call["id"],
            "agent": call["agent"],
            "model": call["model"],
            "response": call["response"][:300],
        }

    steps = []
    for step, (original, fork) in enumerate(pairs, start=1):
        diverged = fork is None or original["response"] != fork["response"]
        steps.append(
            {"step": step, "original": _side(original), "fork": _side(fork), "diverged": diverged}
        )
    for extra, call in enumerate(unpaired_fork, start=len(pairs) + 1):
        steps.append({"step": extra, "original": None, "fork": _side(call), "diverged": True})
    return steps


def compare(fork_task_id: str) -> dict:
    from orchestrator.db.repo import DBLLMCallStore, DBTaskRepo

    repo = DBTaskRepo()
    fork = repo.get_task(fork_task_id)
    if fork is None:
        raise KeyError(f"Task {fork_task_id} not found")
    if not fork.get("replay_of"):
        raise ValueError(f"Task {fork_task_id} is not a replay of anything")
    original = repo.get_task(fork["replay_of"])

    calls = DBLLMCallStore()
    steps = _align_steps(calls.for_task(original["task_id"]), calls.for_task(fork_task_id))

    subtask_outputs = {}
    original_subtasks = {s["sid"]: s.get("output") for s in original["subtasks"]}
    fork_subtasks = {s["sid"]: s.get("output") for s in fork["subtasks"]}
    for sid in sorted(set(original_subtasks) | set(fork_subtasks)):
        subtask_outputs[sid] = {
            "original": original_subtasks.get(sid),
            "fork": fork_subtasks.get(sid),
            "diverged": original_subtasks.get(sid) != fork_subtasks.get(sid),
        }

    return {
        "original_task_id": original["task_id"],
        "fork_task_id": fork_task_id,
        "steps": steps,
        "diverged_steps": sum(1 for s in steps if s["diverged"]),
        "final_output": {
            "original": original.get("final_output"),
            "fork": fork.get("final_output"),
            "diverged": original.get("final_output") != fork.get("final_output"),
        },
        "subtasks": subtask_outputs,
    }
