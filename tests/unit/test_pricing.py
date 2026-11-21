"""What a call is estimated to have cost (PO-9). An estimate is given only when every
figure it needs is known; otherwise it is unknown, never a guess and never zero."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from relay.providers.pricing import PRICES, estimate
from relay.providers.stream import ModelUsage, Terminal, read_stream

RECORDED = Path(__file__).parent.parent / "fixtures" / "claude"
USED = ModelUsage(
    model="claude-opus-5-5",
    input_tokens=1_000_000,
    output_tokens=1_000_000,
    cache_read_tokens=1_000_000,
    cache_write_tokens=2_000_000,
    cache_write_5m_tokens=1_000_000,
    cache_write_1h_tokens=1_000_000,
    provider_cost_usd=None,
)


@pytest.mark.parametrize("recorded", ["success.jsonl", "budget_exhausted.jsonl"])
def test_the_estimate_agrees_with_what_the_provider_billed_for_a_real_call(recorded: str) -> None:
    [ended] = [
        each
        for each in read_stream((RECORDED / recorded).read_text().splitlines())
        if isinstance(each, Terminal)
    ]
    [billed] = ended.usage
    assert billed.provider_cost_usd is not None

    estimated = estimate(PRICES, billed)

    assert estimated is not None
    assert abs(estimated - billed.provider_cost_usd) < Decimal("0.0000001")


def test_each_kind_of_token_has_its_own_price() -> None:
    # input 4, output 20, cache read 0.20, written for five minutes 5, for an hour 8
    assert estimate(PRICES, USED) == Decimal("37.20")


def test_a_model_the_table_does_not_know_has_no_estimate() -> None:
    assert estimate(PRICES, replace(USED, model="claude-opus-9")) is None
    assert estimate(PRICES, replace(USED, model=None)) is None


@pytest.mark.parametrize(
    "missing", ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"]
)
def test_a_counter_that_was_not_reported_leaves_the_estimate_unknown(missing: str) -> None:
    assert estimate(PRICES, replace(USED, **{missing: None})) is None


def test_cache_writes_without_their_lifetimes_cannot_be_priced() -> None:
    """They cost different amounts by lifetime (S3-18.10)."""
    unsplit = replace(USED, cache_write_5m_tokens=None, cache_write_1h_tokens=None)

    assert estimate(PRICES, unsplit) is None
    assert estimate(PRICES, replace(unsplit, cache_write_tokens=0)) == Decimal("24.20")


def test_lifetimes_that_do_not_add_up_to_what_was_written_cannot_be_priced() -> None:
    assert estimate(PRICES, replace(USED, cache_write_tokens=5_000_000)) is None


def test_a_model_priced_by_the_size_of_the_prompt_is_estimated_only_below_the_threshold() -> None:
    small = replace(
        USED,
        model="claude-haiku-5-5",
        input_tokens=50_000,
        output_tokens=1_000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cache_write_5m_tokens=0,
        cache_write_1h_tokens=0,
    )

    assert estimate(PRICES, small) == Decimal("0.0055")
    # Over the threshold in total, some request may have been priced at the higher
    # rate, and the totals do not say which.
    assert estimate(PRICES, replace(small, input_tokens=150_000)) is None


def test_the_table_says_which_prices_it_is() -> None:
    assert PRICES.version == "2026-11-03"
    assert "claude-fable-5-2" in PRICES.rates
