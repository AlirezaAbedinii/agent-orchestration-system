"""Task decomposition engine: request → validated ExecutionPlan.

Invalid structured output is retried exactly once with the validation error
appended to the prompt (plan §5 Phase 1.2).
"""

from __future__ import annotations

from orchestrator.llm.clients import LLMClient
from orchestrator.llm.router import SPECIALISTS
from orchestrator.llm.structured import StructuredOutputError, parse_structured
from orchestrator.planning.schemas import ExecutionPlan

# Stable marker; mock fixtures and tests match on it.
PLAN_MARKER = "Create an execution plan"

PLAN_PROMPT = """You are the supervisor of a multi-agent system. {marker} for the task below.

Task: {request}

Available specialists: {specialists} —
research (web research, source gathering), analysis (data extraction/computation),
writing (drafts, summaries, memos), code (writes and runs code).

Decompose the task into subtasks. Respond with ONLY a JSON object:
{{
  "task_summary": "<one sentence>",
  "subtasks": [
    {{
      "id": "s1",
      "description": "<what to do>",
      "specialist": "research|analysis|writing|code",
      "required_inputs": ["<what it needs>"],
      "expected_output_format": "<format>",
      "estimated_complexity": "low|medium|high",
      "depends_on": []
    }}
  ],
  "confidence": <0.0-1.0, your confidence this plan solves the task>
}}

Rules: subtask ids are unique; depends_on may only reference other subtask ids and
must form no cycles; make independent subtasks so they can run in parallel.
"""

RETRY_SUFFIX = """

Your previous plan was invalid and has been rejected. Validation error:
{error}

Produce a corrected JSON plan that fixes this error. Respond with ONLY the JSON object.
"""


class PlanValidationError(ValueError):
    pass


def decompose(llm: LLMClient, request: str, memories: str | None = None) -> ExecutionPlan:
    prompt = PLAN_PROMPT.format(marker=PLAN_MARKER, request=request, specialists=", ".join(SPECIALISTS))
    if memories:
        prompt = f"{prompt}\n{memories}\n"
    response = llm.complete("supervisor", prompt)
    try:
        return parse_structured(response.text, ExecutionPlan)
    except StructuredOutputError as first_error:
        retry_prompt = prompt + RETRY_SUFFIX.format(error=first_error)
        retry_response = llm.complete("supervisor", retry_prompt)
        try:
            return parse_structured(retry_response.text, ExecutionPlan)
        except StructuredOutputError as second_error:
            raise PlanValidationError(
                f"Plan invalid after retry: {second_error}"
            ) from second_error
