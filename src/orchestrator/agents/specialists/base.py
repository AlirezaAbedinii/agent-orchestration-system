"""Specialist agents: a bounded tool-use loop driven by structured actions.

Each iteration the specialist either calls one of its permitted tools (routed
through the registry, which enforces ownership and rate limits) or returns its
final output. Tool errors propagate to the graph node, which counts the attempt
as failed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, model_validator

from orchestrator.agents.base import BaseAgent
from orchestrator.config import get_settings
from orchestrator.llm.clients import LLMClient
from orchestrator.llm.structured import StructuredOutputError, parse_structured
from orchestrator.tools.base import ToolContext
from orchestrator.tools.registry import ToolRegistry

# Stable prompt markers; mock fixtures and tests match on these exact strings.
EMPTY_TRANSCRIPT = "Transcript: (none yet)"
FEEDBACK_MARKER = "Reviewer feedback:"
TOOL_RESULT_PREFIX = "-> {tool} returned:"

SPECIALIST_PROMPT = """You are the {name} specialist in a multi-agent system. {role}

Subtask: {description}
Expected output format: {expected_format}
Inputs from completed subtasks:
{inputs}

Available tools:
{tools}
{feedback_block}
{transcript}

Respond with ONLY one JSON object:
- to call a tool: {{"action": "tool", "tool": "<tool name>", "arguments": {{...}}}}
- when done:     {{"action": "final", "output": "<your deliverable>"}}
"""


class SpecialistError(RuntimeError):
    pass


class SpecialistAction(BaseModel):
    action: Literal["tool", "final"]
    tool: str | None = None
    arguments: dict = {}
    output: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "SpecialistAction":
        if self.action == "tool" and not self.tool:
            raise ValueError("action=tool requires a tool name")
        if self.action == "final" and self.output is None:
            raise ValueError("action=final requires an output")
        return self


@dataclass
class SpecialistResult:
    output: str
    tool_calls: list[str] = field(default_factory=list)


class SpecialistAgent(BaseAgent):
    ROLE = ""

    def __init__(self, llm: LLMClient, registry: ToolRegistry):
        super().__init__(llm)
        self.registry = registry

    def _prompt(
        self, spec: dict, inputs: dict[str, str], feedback: str | None, transcript: list[str]
    ) -> str:
        rendered_inputs = (
            "\n".join(f"[{sid}] {text}" for sid, text in sorted(inputs.items())) or "(none)"
        )
        feedback_block = f"\n{FEEDBACK_MARKER} {feedback}\n" if feedback else ""
        transcript_block = (
            "Transcript:\n" + "\n".join(transcript) if transcript else EMPTY_TRANSCRIPT
        )
        return SPECIALIST_PROMPT.format(
            name=self.name,
            role=self.ROLE,
            description=spec["description"],
            expected_format=spec.get("expected_output_format", "plain text"),
            inputs=rendered_inputs,
            tools=self.registry.describe_for(self.name),
            feedback_block=feedback_block,
            transcript=transcript_block,
        )

    def _next_action(self, prompt: str) -> SpecialistAction:
        response = self.complete(prompt)
        try:
            return parse_structured(response.text, SpecialistAction)
        except StructuredOutputError as error:
            retry = self.complete(
                prompt + f"\n\nYour previous reply could not be parsed ({error}). "
                "Respond with ONLY the JSON object."
            )
            return parse_structured(retry.text, SpecialistAction)

    def execute(
        self, spec: dict, inputs: dict[str, str], feedback: str | None, ctx: ToolContext
    ) -> SpecialistResult:
        transcript: list[str] = []
        tool_calls: list[str] = []
        for _ in range(get_settings().max_tool_iterations):
            action = self._next_action(self._prompt(spec, inputs, feedback, transcript))
            if action.action == "final":
                return SpecialistResult(output=action.output or "", tool_calls=tool_calls)
            result = self.registry.invoke(action.tool, action.arguments, ctx)
            tool_calls.append(action.tool)
            rendered = json.dumps(result)[:2000]
            transcript.append(f"{TOOL_RESULT_PREFIX.format(tool=action.tool)} {rendered}")
        raise SpecialistError(
            f"{self.name} exceeded {get_settings().max_tool_iterations} tool iterations"
        )
