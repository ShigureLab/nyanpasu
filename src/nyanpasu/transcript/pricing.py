from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nyanpasu.transcript.models import ModelUsageTotals


@dataclass(frozen=True, slots=True)
class TokenPrice:
    input: float
    cached_input: float
    cache_write: float
    output: float


# USD per million text tokens. OpenAI Standard API rates with 30-minute cache writes.
# https://developers.openai.com/api/docs/pricing
OPENAI_PRICES = {
    "gpt-6.2-astra": TokenPrice(input=8, cached_input=0.4, cache_write=10, output=40),
    "gpt-6.2-sol": TokenPrice(input=2, cached_input=0.1, cache_write=2.5, output=10),
    "gpt-6-astra": TokenPrice(input=10, cached_input=1, cache_write=12.5, output=50),
    "gpt-6.1-sol": TokenPrice(input=2, cached_input=0.1, cache_write=2.5, output=10),
    "gpt-6.1-terra": TokenPrice(input=1, cached_input=0.05, cache_write=1.25, output=5),
    "gpt-6-sol": TokenPrice(input=2, cached_input=0.2, cache_write=2.5, output=10),
    "gpt-6-luna": TokenPrice(input=0.10, cached_input=0.01, cache_write=0.125, output=0.50),
}

# DeepSeek peak API list rates. Cache writes are covered by the cache-miss input rate.
# https://api-docs.deepseek.com/quick_start/pricing/
DEEPSEEK_PRICES = {
    "deepseek-flash": TokenPrice(input=0.3, cached_input=0.006, cache_write=0.3, output=1.2),
    "deepseek-v4-pro": TokenPrice(input=1.32, cached_input=0.044, cache_write=1.32, output=3.96),
}
TOKEN_PRICES = {"openai": OPENAI_PRICES, "deepseek": DEEPSEEK_PRICES}


def estimate_api_cost(provider: str | None, model: str | None, usage: ModelUsageTotals) -> float | None:
    prices = TOKEN_PRICES.get(provider) if provider is not None else None
    price = prices.get(model) if prices is not None and model is not None else None
    if price is None:
        return None
    uncached = usage.input_tokens - usage.cached_input_tokens - usage.cache_write_tokens
    input_cost = (
        uncached * price.input
        + usage.cached_input_tokens * price.cached_input
        + usage.cache_write_tokens * price.cache_write
    )
    output_cost = usage.output_tokens * price.output
    if provider == "openai" and usage.input_tokens > 272_000:
        input_cost *= 2
        output_cost *= 1.5
    return (input_cost + output_cost) / 1_000_000
