"""What the model has cost: tokens by kind, and their price.

Prices are US dollars per million tokens. A model that is not listed has no
price here, and its cost is reported as unknown instead of guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# (input, cache write with the 5-minute TTL, cache read, output)
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-5-5": (2.00, 2.50, 0.20, 10.00),
    "claude-opus-5-5": (4.00, 5.00, 0.20, 20.00),
    "claude-haiku-4-5": (1.00, 1.25, 0.10, 5.00),
}


@dataclass
class Usage:
    # Input the model read at full price: whatever was neither written to the cache nor read from it.
    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    # Includes the model's reasoning, which is billed as output.
    output: int = 0
    calls: int = 0

    def add(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.input += getattr(usage, "input_tokens", 0) or 0
        self.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.output += getattr(usage, "output_tokens", 0) or 0
        self.calls += 1

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input + other.input,
            self.cache_write + other.cache_write,
            self.cache_read + other.cache_read,
            self.output + other.output,
            self.calls + other.calls,
        )


def cost_usd(model: str, usage: Usage) -> Optional[float]:
    prices = PRICES.get(model)
    if prices is None:
        return None
    fresh, write, read, output = prices
    tokens = usage.input * fresh + usage.cache_write * write + usage.cache_read * read + usage.output * output
    return tokens / 1_000_000
