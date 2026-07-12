"""Reviewer agent: scores specialist outputs 1–5 with feedback.

Routed to a different provider than the producing agent (llm/router.py).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from orchestrator.agents.base import BaseAgent
from orchestrator.llm.structured import StructuredOutputError, parse_structured

# Stable marker; mock fixtures and tests match on it.
REVIEW_MARKER = "Review the following specialist output"

REVIEW_PROMPT = """{marker} for quality before it is returned to the supervisor.

Subtask: {description}
Expected output format: {expected_format}

Specialist output:
---
{output}
---

Score the output 1-5 (5 = excellent, meets the subtask and format; 1 = unusable).
Respond with ONLY JSON: {{"score": <1-5>, "feedback": "<what is wrong or missing, if anything>"}}
"""

RETRY_SUFFIX = """

Your previous reply could not be parsed ({error}). Respond with ONLY the JSON object.
"""


class ReviewVerdict(BaseModel):
    score: int = Field(ge=1, le=5)
    feedback: str = ""


class Reviewer(BaseAgent):
    name = "reviewer"

    def review(
        self,
        description: str,
        expected_format: str,
        output: str,
        *,
        producer_provider: str | None = None,
    ) -> ReviewVerdict:
        prompt = REVIEW_PROMPT.format(
            marker=REVIEW_MARKER,
            description=description,
            expected_format=expected_format,
            output=output,
        )
        response = self.complete(prompt, producer_provider=producer_provider)
        try:
            return parse_structured(response.text, ReviewVerdict)
        except StructuredOutputError as error:
            retry = self.complete(prompt + RETRY_SUFFIX.format(error=error), producer_provider=producer_provider)
            return parse_structured(retry.text, ReviewVerdict)
