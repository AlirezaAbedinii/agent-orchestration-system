"""Supervisor agent: task decomposition and final synthesis."""

from __future__ import annotations

from orchestrator.agents.base import BaseAgent
from orchestrator.planning.decomposer import decompose
from orchestrator.planning.schemas import ExecutionPlan

# Stable marker; mock fixtures and tests match on it.
SYNTH_MARKER = "Synthesize the final deliverable"

SYNTH_PROMPT = """{marker} for the task below from the completed subtask outputs.

Task: {request}

Completed subtask outputs:
{outputs}

Write the final deliverable for the user. Respond with the deliverable text only.
"""


class Supervisor(BaseAgent):
    name = "supervisor"

    def plan(self, request: str) -> ExecutionPlan:
        return decompose(self.llm, request)

    def synthesize(self, request: str, outputs: dict[str, str]) -> str:
        rendered = "\n".join(f"[{sid}]\n{text}\n" for sid, text in sorted(outputs.items()))
        prompt = SYNTH_PROMPT.format(marker=SYNTH_MARKER, request=request, outputs=rendered)
        return self.complete(prompt).text
