"""An estimate of what a call cost, from a dated table of list prices (PO-9).

Relay shows three kinds of figure and never mixes them: what the provider said it
charged (observed), what this table makes of the tokens reported (estimated), and
unknown. An estimate is given only when everything it needs is known. The old Relay
priced a model it did not know as if it were another; this one does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from relay.providers.stream import ModelUsage

_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class Rate:
    """Dollars per million tokens. Cache reads have a price of their own for each
    model: it is not a fixed share of the input price."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    # Some models charge more for a request whose prompt is over this many tokens. The
    # totals of a call do not say how large each request was, so above it in total the
    # estimate is unknown.
    flat_up_to: int | None = None


@dataclass(frozen=True)
class PriceTable:
    version: str
    source: str
    rates: dict[str, Rate]
    # What writing to the cache costs, as a multiple of the input price, by how long
    # the entry is kept.
    cache_write_5m: Decimal
    cache_write_1h: Decimal


def _rate(input: str, output: str, cache_read: str, flat_up_to: int | None = None) -> Rate:
    return Rate(Decimal(input), Decimal(output), Decimal(cache_read), flat_up_to)


PRICES = PriceTable(
    version="2026-11-03",
    source=(
        "Anthropic first-party list prices as published on 2026-11-03. The cache-read "
        "price of claude-haiku-5-5 and the one-hour cache-write multiple are as billed "
        "on a real call on 2026-11-21."
    ),
    rates={
        "claude-fable-5-2": _rate("8", "40", "0.20"),
        "claude-opus-5-5": _rate("4", "20", "0.20"),
        "claude-sonnet-5-5": _rate("2", "10", "0.10"),
        "claude-haiku-5-5": _rate("0.10", "0.50", "0.01", flat_up_to=100_000),
    },
    cache_write_5m=Decimal("1.25"),
    cache_write_1h=Decimal(2),
)


def estimate(table: PriceTable, used: ModelUsage) -> Decimal | None:
    """The estimated cost in dollars, or None where it cannot be known."""
    rate = table.rates.get(used.model) if used.model is not None else None
    if rate is None:
        return None
    if None in (
        used.input_tokens,
        used.output_tokens,
        used.cache_read_tokens,
        used.cache_write_tokens,
    ):
        return None
    assert used.input_tokens is not None and used.output_tokens is not None
    assert used.cache_read_tokens is not None and used.cache_write_tokens is not None
    for_five_minutes, for_an_hour = used.cache_write_5m_tokens, used.cache_write_1h_tokens
    if used.cache_write_tokens == 0:
        for_five_minutes = for_an_hour = 0
    if for_five_minutes is None or for_an_hour is None:
        return None
    if for_five_minutes + for_an_hour != used.cache_write_tokens:
        return None
    prompt = used.input_tokens + used.cache_read_tokens + used.cache_write_tokens
    if rate.flat_up_to is not None and prompt > rate.flat_up_to:
        return None
    return (
        used.input_tokens * rate.input
        + used.output_tokens * rate.output
        + used.cache_read_tokens * rate.cache_read
        + for_five_minutes * rate.input * table.cache_write_5m
        + for_an_hour * rate.input * table.cache_write_1h
    ) / _MILLION
