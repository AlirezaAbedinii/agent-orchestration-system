"""Helpers for parsing structured (JSON) agent output."""

from __future__ import annotations

import json
from typing import TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(ValueError):
    """Raised when LLM text cannot be parsed into the expected schema."""


def extract_json(text: str) -> str:
    """Return the first JSON object embedded in *text*.

    Tolerates markdown code fences and prose around the object.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    start = stripped.find("{")
    if start == -1:
        raise StructuredOutputError(f"No JSON object found in: {text[:200]!r}")
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(stripped[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return stripped[start : i + 1]
    raise StructuredOutputError(f"Unbalanced JSON object in: {text[:200]!r}")


def parse_structured(text: str, model_cls: type[T]) -> T:
    try:
        raw = extract_json(text)
        return model_cls.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise StructuredOutputError(str(exc)) from exc
