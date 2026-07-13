"""Static price table (USD per million tokens) and cost computation.

Model names are normalized before lookup: provider prefixes ("openai:",
"anthropic:") and the mock fixture prefix ("mock-") are stripped, so recorded
mock runs price like the model they stand in for. Replayed calls
("replay:...") always cost zero — nothing was spent.
"""

from __future__ import annotations

# (input, output) USD per 1M tokens
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "text-embedding-3-small": (0.02, 0.0),
}


def normalize_model(model: str) -> str:
    if ":" in model:
        model = model.split(":", 1)[1]
    if model.startswith("mock-"):
        model = model.removeprefix("mock-")
    return model


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    if model.startswith("replay:"):
        return 0.0
    prices = PRICES_PER_MTOK.get(normalize_model(model))
    if prices is None:
        return 0.0
    input_price, output_price = prices
    return round(
        prompt_tokens * input_price / 1_000_000 + completion_tokens * output_price / 1_000_000, 10
    )
