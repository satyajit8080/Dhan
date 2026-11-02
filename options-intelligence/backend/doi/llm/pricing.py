"""Model tiers, price book and cost arithmetic.

Verified from official xAI sources reachable from this environment
(github.com/xai-org/xai-proto @7c1372f, xai-sdk-python @884ba42):
  - usage fields: prompt_tokens, completion_tokens, reasoning_tokens,
    cached_prompt_text_tokens, cost_in_usd_ticks (1 tick = 1e-10 USD);
  - model listing prices: prompt/completion "in 1/100 USD cents per 1M tokens",
    cached prompt "in USD cents per 100M tokens".
UNVERIFIED (docs.x.ai unreachable): current prices, which model names are live,
and whether REST responses include cost_in_usd_ticks. So the seeded prices are
deliberately pessimistic and marked unverified. `refresh_from_listing` replaces
them with the provider's own numbers when an API key is available.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

D = Decimal
Tier = Literal["fast", "reasoning"]
USD_PER_TICK = D("1e-10")


@dataclass(frozen=True, slots=True)
class ModelPrice:
    model: str
    input_per_m: Decimal
    cached_input_per_m: Decimal
    output_per_m: Decimal
    source: str
    verified: bool


@dataclass(frozen=True, slots=True)
class TierModels:
    fast: str = "grok-4.20-non-reasoning"  # name present in official SDK examples; availability UNVERIFIED
    reasoning: str = "grok-4.20-reasoning"  # idem


# Pessimistic seeds (upper ends of third-party ranges, Nov 2026). Overestimating
# keeps the hard cap safe; settlement uses provider-reported cost when present.
SEED_PRICES = {
    "grok-4.20-non-reasoning": ModelPrice("grok-4.20-non-reasoning", D("1.25"), D("0.30"), D("2.50"), "seed-unverified", False),
    "grok-4.20-reasoning": ModelPrice("grok-4.20-reasoning", D("3.00"), D("0.75"), D("15.00"), "seed-unverified", False),
}


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    cost_ticks: int | None = None


@dataclass(frozen=True, slots=True)
class CostModel:
    fx_inr_per_usd: Decimal = D("90")  # UNVERIFIED rate; conservative
    tax_multiplier: Decimal = D("1.18")  # GST buffer on foreign services (treatment UNVERIFIED)
    reasoning_allowance: dict[str, int] | None = None  # per tier, reserved on top of max_tokens

    def inr(self, usd: Decimal) -> Decimal:
        return (usd * self.fx_inr_per_usd * self.tax_multiplier).quantize(D("0.000001"))

    def allowance(self, tier: Tier) -> int:
        return (self.reasoning_allowance or {"fast": 0, "reasoning": 4000})[tier]


def estimate_prompt_tokens(text: str) -> int:
    """Upper-bound heuristic: about 3 characters per token, plus a fixed overhead per message."""
    return math.ceil(len(text) / 3) + 32


def usd_estimate(price: ModelPrice, prompt_tokens: int, max_output: int) -> Decimal:
    """Worst case: no cache discount, full output and reasoning allowance billed."""
    return (D(prompt_tokens) * price.input_per_m + D(max_output) * price.output_per_m) / D(1_000_000)


def usd_actual(price: ModelPrice, u: Usage) -> tuple[Decimal, str]:
    if u.cost_ticks is not None:
        return D(u.cost_ticks) * USD_PER_TICK, "PROVIDER_TICKS"
    cached = min(u.cached_tokens, u.prompt_tokens)
    # completion + reasoning both billed at the output rate. If the provider's completion
    # count already includes reasoning this overestimates, which is the safe direction.
    usd = (
        D(u.prompt_tokens - cached) * price.input_per_m
        + D(cached) * price.cached_input_per_m
        + D(u.completion_tokens + u.reasoning_tokens) * price.output_per_m
    ) / D(1_000_000)
    return usd, "PRICE_BOOK"


def parse_usage(raw: dict | None) -> Usage:
    """Accepts OpenAI-style REST usage and xAI proto-style field names."""
    raw = raw or {}
    ptd = raw.get("prompt_tokens_details") or {}
    ctd = raw.get("completion_tokens_details") or {}
    ticks = raw.get("cost_in_usd_ticks")
    return Usage(
        prompt_tokens=int(raw.get("prompt_tokens") or 0),
        completion_tokens=int(raw.get("completion_tokens") or 0),
        cached_tokens=int(ptd.get("cached_tokens") or raw.get("cached_prompt_text_tokens") or 0),
        reasoning_tokens=int(ctd.get("reasoning_tokens") or raw.get("reasoning_tokens") or 0),
        cost_ticks=int(ticks) if ticks is not None else None,
    )


def prices_from_listing(models: list[dict]) -> dict[str, ModelPrice]:
    """Convert a /v1/language-models listing (proto units) into USD per million tokens."""
    out = {}
    for m in models:
        try:
            p_in = D(m["prompt_text_token_price"]) / D(10_000)  # 1/100 cents per 1M -> USD per 1M
            p_out = D(m["completion_text_token_price"]) / D(10_000)
            p_cached = D(m.get("cached_prompt_token_price", m["prompt_text_token_price"])) / D(
                10_000
            )  # cents per 100M -> USD per 1M
        except (KeyError, ArithmeticError, TypeError, ValueError):
            continue
        for name in [m.get("id") or m.get("name"), *(m.get("aliases") or [])]:
            if name:
                out[name] = ModelPrice(name, p_in, p_cached, p_out, "xai:/v1/language-models", True)
    return out
