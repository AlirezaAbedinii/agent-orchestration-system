"""Post-completion memory extraction: task → episode / facts / preferences."""

from __future__ import annotations

from pydantic import BaseModel

from orchestrator.llm.clients import LLMClient
from orchestrator.llm.structured import StructuredOutputError, parse_structured
from orchestrator.memory.longterm import LongTermMemory

# Stable marker; mock fixtures match on it.
EXTRACT_MARKER = "Extract long-term memories"

EXTRACT_PROMPT = """{marker} from this completed task so future planning improves.

Task request: {request}

Subtask outputs:
{outputs}

Tools used per subtask: {tools}

Final deliverable:
{final}

Respond with ONLY JSON:
{{"episode": "<one paragraph: what was asked, the approach that worked, the tools used>",
  "facts": ["<domain fact discovered>", "..."],
  "preferences": ["<user preference observed>", "..."]}}
Use empty lists when nothing applies.
"""

RETRY_SUFFIX = """

Your previous reply could not be parsed ({error}). Respond with ONLY the JSON object.
"""


class ExtractedMemories(BaseModel):
    episode: str
    facts: list[str] = []
    preferences: list[str] = []


def extract_memories(
    llm: LLMClient, *, request: str, outputs: dict[str, str], tools_used: dict[str, list[str]], final_output: str
) -> ExtractedMemories:
    prompt = EXTRACT_PROMPT.format(
        marker=EXTRACT_MARKER,
        request=request,
        outputs="\n".join(f"[{sid}] {text}" for sid, text in sorted(outputs.items())) or "(none)",
        tools={sid: calls for sid, calls in sorted(tools_used.items())},
        final=final_output,
    )
    response = llm.complete("memory", prompt)
    try:
        return parse_structured(response.text, ExtractedMemories)
    except StructuredOutputError as error:
        retry = llm.complete("memory", prompt + RETRY_SUFFIX.format(error=error))
        return parse_structured(retry.text, ExtractedMemories)


def store_extracted(
    longterm: LongTermMemory,
    extracted: ExtractedMemories,
    *,
    user_id: str,
    task_id: str,
    events=None,
) -> list[tuple[str, str]]:
    """Persist extracted memories; returns (kind, memory_id) pairs."""
    stored: list[tuple[str, str]] = []
    batches = (
        ("episodes", [extracted.episode] if extracted.episode.strip() else []),
        ("facts", extracted.facts),
        ("preferences", extracted.preferences),
    )
    for kind, texts in batches:
        for text in texts:
            memory_id = longterm.add(kind, text, user_id=user_id, task_id=task_id)
            if events is not None:
                events.record(
                    user_id=user_id, memory_id=memory_id, kind=kind, action="created", task_id=task_id
                )
            stored.append((kind, memory_id))
    return stored
