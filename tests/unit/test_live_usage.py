"""The live account of what calls have used: four things kept apart (3C-1.9)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from relay.application.usage import UNKNOWN, account_of, account_text


def call(
    role: str = "builder",
    state: str = "applied",
    usage: list[dict[str, Any]] | None = None,
    charged: str | None = None,
    estimated: str | None = None,
    table: str | None = "2026-11",
) -> dict[str, Any]:
    return {
        "role": role,
        "state": state,
        "usage": usage,
        "provider_cost_usd": Decimal(charged) if charged is not None else None,
        "estimated_cost_usd": Decimal(estimated) if estimated is not None else None,
        "price_table": table if estimated is not None else None,
    }


def billed(model: str, **counters: int | None) -> dict[str, Any]:
    whole = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    return {"model": model, **whole, **counters}


def test_a_call_billed_to_two_models_is_one_call_and_each_models_tokens_are_its_own() -> None:
    account = account_of(
        [
            call(
                usage=[billed("opus", input_tokens=100), billed("haiku", input_tokens=7)],
                charged="0.30",
                estimated="0.28",
            ),
            call(
                role="qa", usage=[billed("haiku", input_tokens=3)], charged="0.02", estimated="0.02"
            ),
        ]
    )

    assert account["calls"] == 2  # not three: one of them was billed to two models
    assert account["by_role"] == {"builder": 1, "qa": 1}
    assert account["by_model"]["opus"] == {
        "calls": 1,
        "input_tokens": 100,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert (
        account["by_model"]["haiku"]["calls"],
        account["by_model"]["haiku"]["input_tokens"],
    ) == (
        2,
        10,
    )
    # The provider's charge is the call's, whole: it is in the total, and under no model.
    assert account["charged_by_the_provider"] == {
        "usd": "0.32",
        "calls_it_said_the_cost_of": 2,
        "calls_it_did_not": 0,
    }
    assert all("usd" not in each for each in account["by_model"].values())


def test_a_counter_that_was_not_reported_is_unknown_and_a_reported_zero_is_a_zero() -> None:
    account = account_of(
        [
            call(usage=[billed("opus", cache_read_tokens=None)]),
            call(usage=[billed("opus", cache_read_tokens=40, output_tokens=0)]),
        ]
    )

    opus = account["by_model"]["opus"]
    assert opus["cache_read_tokens"] == UNKNOWN  # not 40: one call did not say
    assert (opus["input_tokens"], opus["output_tokens"]) == (20, 5)
    assert account["calls_with_no_use_reported"] == 0


def test_estimates_made_by_different_price_tables_are_not_added_to_each_other() -> None:
    account = account_of(
        [
            call(usage=[billed("opus")], estimated="0.10", table="2026-10"),
            call(usage=[billed("opus")], estimated="0.20", table="2026-11"),
            call(usage=[billed("opus")], estimated="0.05", table="2026-11"),
            call(usage=[billed("opus")]),  # a model the table has no price for
        ]
    )

    assert account["estimated_by_relay"] == [
        {"price_table": "2026-10", "usd": "0.1", "calls": 1},
        {"price_table": "2026-11", "usd": "0.25", "calls": 2},
    ]
    assert account["calls_with_no_estimate"] == 1
    assert account["charged_by_the_provider"]["calls_it_did_not"] == 4
    text = account_text({"project": "vat", "feature": None, **account})
    assert text.count("an estimate, price table") == 2  # each said to be one, with its table


def test_calls_that_have_not_ended_or_never_started_are_counted_apart_and_add_nothing() -> None:
    account = account_of(
        [
            call(usage=[billed("opus")], charged="0.10"),
            call(state="intended"),
            call(state="uncertain"),
            call(state="not_started"),
            call(state="errored"),  # ended, and reported nothing of what it used
        ]
    )

    assert account["calls"] == 2
    assert account["calls_in_flight"] == 1
    assert account["calls_whose_end_is_not_known"] == 1
    assert account["calls_that_never_started"] == 1
    assert account["calls_with_no_use_reported"] == 1
    assert account["charged_by_the_provider"]["usd"] == "0.1"
    assert account["by_model"]["opus"]["calls"] == 1


def test_no_calls_is_an_account_of_nothing_and_says_so() -> None:
    account = account_of([])

    assert account["calls"] == 0 and account["by_model"] == {} and account["by_role"] == {}
    assert account["charged_by_the_provider"]["usd"] == "0"
    assert "calls made: 0" in account_text({"project": "vat", "feature": "F1", **account})
