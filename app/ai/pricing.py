"""The USD cost of a Claude call from its usage (pure; no SDK import).

Prices are per million tokens: input, output and cache read from Settings.claude_price
(built-in list prices, overridable by AI_MODEL_PRICES). A cache write is billed at 1.25x the
input price (5-minute TTL). Claude Haiku 5.5's built-in price applies up to 100K prompt tokens;
a longer prompt is billed at its higher tier. Thinking tokens are output tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.config import CLAUDE_PRICES, Settings

MILLION = Decimal(1_000_000)
CACHE_WRITE_FACTOR = Decimal("1.25")
HAIKU = "claude-haiku-5-5"
HAIKU_LONG_PROMPT = 100_000
HAIKU_LONG_PRICE = (Decimal("0.50"), Decimal("2.50"), Decimal("0.05"))
# Characters per token used to estimate a prompt before it is sent (conservative).
CHARS_PER_TOKEN = 3


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


class NotPriced(LookupError):
    """A model with no price: it is never called (no price, no budget control)."""


def price_for(
    settings: Settings, model: str, prompt_tokens: int = 0
) -> tuple[Decimal, Decimal, Decimal]:
    price = settings.claude_price(model)
    if price is None:
        raise NotPriced(model)
    own = model in settings.AI_MODEL_PRICES
    if model == HAIKU and not own and prompt_tokens > HAIKU_LONG_PROMPT:
        return HAIKU_LONG_PRICE
    return price


def cost_usd(settings: Settings, model: str, usage: TokenUsage) -> Decimal:
    p_in, p_out, p_read = price_for(settings, model, usage.prompt_tokens)
    total = (
        usage.input_tokens * p_in
        + usage.output_tokens * p_out
        + usage.cache_read_tokens * p_read
        + usage.cache_write_tokens * p_in * CACHE_WRITE_FACTOR
    ) / MILLION
    return total.quantize(Decimal("0.000001"))


def worst_case_usd(settings: Settings, model: str, prompt_chars: int, max_tokens: int) -> Decimal:
    """The most a call can cost: the whole prompt written to the cache (the dearest input
    rate) plus every output token. Reserved against the budget before the call."""
    prompt_tokens = prompt_chars // CHARS_PER_TOKEN + 1
    return cost_usd(
        settings,
        model,
        TokenUsage(output_tokens=max_tokens, cache_write_tokens=prompt_tokens),
    )


__all__ = [
    "CLAUDE_PRICES",
    "NotPriced",
    "TokenUsage",
    "cost_usd",
    "price_for",
    "worst_case_usd",
]
