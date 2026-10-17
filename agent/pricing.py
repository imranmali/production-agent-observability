"""Token -> USD cost.

Prices are USD per million tokens, first-party Claude API list prices as of
October 2026. Verify against https://www.anthropic.com/pricing before relying on
the cost dashboards for billing; Bedrock/Vertex prices differ.
Cache writes are billed at 1.25x input (5-minute TTL).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float

    @property
    def cache_write(self) -> float:
        return self.input * 1.25


PRICES: dict[str, Price] = {
    "claude-fable-5-1": Price(10.00, 50.00, 1.00),
    "claude-opus-5-5": Price(4.00, 20.00, 0.20),
    "claude-opus-5": Price(5.00, 25.00, 0.50),
    "claude-opus-4-8": Price(5.00, 25.00, 0.50),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.20),
    "claude-sonnet-5": Price(2.00, 10.00, 0.20),
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01),
}


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )


def cost_usd(model: str, usage: Usage) -> float:
    """Cost of one call. Unknown models cost 0 and are flagged by the
    `agent_unpriced_model_calls_total` metric rather than guessed."""
    price = PRICES.get(model)
    if price is None:
        return 0.0
    return (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * price.cache_read
        + usage.cache_write_tokens * price.cache_write
    ) / 1_000_000


def is_priced(model: str) -> bool:
    return model in PRICES
