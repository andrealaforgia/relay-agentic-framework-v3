"""The live account of what calls have used (3C-1.9, PO-8, PO-9): who spent it, and four
things kept apart that would say something untrue if added together."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from relay.application.final_checks import totals_of
from relay.application.usage import UNKNOWN, account_of, account_text, summary_text


def call(
    role: str = "builder",
    state: str = "applied",
    usage: list[dict[str, Any]] | None = None,
    charged: str | None = None,
    estimated: str | None = None,
    table: str | None = "2026-11",
    billed_by_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "role": role,
        "state": state,
        "usage": usage,
        "provider_cost_usd": Decimal(charged) if charged is not None else None,
        "estimated_cost_usd": Decimal(estimated) if estimated is not None else None,
        "price_table": table if estimated is not None else None,
        "billed_by_model": billed_by_model,
    }


def billed(model: str, cost: str | None = None, **counters: int | None) -> dict[str, Any]:
    whole = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    return {"model": model, **whole, **counters, "provider_cost_usd": cost}


def shown(account: dict[str, Any]) -> str:
    return account_text({"project": "vat", "feature": None, **account})


def test_two_roles_on_one_model_each_have_their_own_spend_and_a_whole_calls_charge_is_counted_once() -> (
    None
):
    """3C-S3-1.1, PO-8: the Builder and QA use the same model, and one of the Builder's
    calls is also billed to a second. Who spent what is told apart."""
    account = account_of(
        [
            call(
                role="builder",
                usage=[
                    billed("opus", cost="0.25", input_tokens=100),
                    billed("haiku", cost="0.05", input_tokens=7),
                ],
                charged="0.30",
                estimated="0.28",
                table="2026-11",
            ),
            call(
                role="builder",
                usage=[billed("opus", cost="0.10")],
                charged="0.10",
                estimated="0.09",
                table="2026-10",
            ),
            call(
                role="qa",
                usage=[billed("opus", cost="0.02", input_tokens=3)],
                charged="0.02",
                estimated="0.02",
            ),
        ]
    )

    assert account["calls"] == 3  # not four: one of them was billed to two models
    builder, qa = account["by_role"]["builder"], account["by_role"]["qa"]
    assert (builder["calls"], builder["input_tokens"], builder["output_tokens"]) == (2, 117, 15)
    assert (qa["calls"], qa["input_tokens"]) == (1, 3)
    assert builder["charged_by_the_provider"]["usd"] == "0.4"
    assert qa["charged_by_the_provider"]["usd"] == "0.02"
    assert builder["estimated_by_relay"] == [
        {"price_table": "2026-10", "usd": "0.09", "calls": 1},
        {"price_table": "2026-11", "usd": "0.28", "calls": 1},
    ]
    # The whole call's charge is in the total once; what the provider attributed to each
    # model is shown under that model, and is not added to it again.
    assert account["charged_by_the_provider"] == {
        "usd": "0.42",
        "calls_it_said_the_cost_of": 3,
        "calls_it_did_not": 0,
    }
    assert account["by_model"]["opus"]["calls"] == 3
    assert account["by_model"]["opus"]["charged_usd"] == "0.37"
    assert account["by_model"]["haiku"] == {
        "calls": 1,
        "input_tokens": 7,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "charged_usd": "0.05",
    }
    text = shown(account)
    assert "the builder: 2 call(s)" in text and "the qa: 1 call(s)" in text
    assert "the provider's charge for this model: 0.37 USD" in text


def test_a_models_charge_is_read_from_the_providers_own_breakdown_where_the_call_predates_keeping_it() -> (
    None
):
    account = account_of(
        [
            call(
                usage=[billed("opus")],
                charged="0.10",
                billed_by_model={"opus": {"costUSD": 0.1}},
            ),
            call(usage=[billed("haiku")], charged="0.01"),  # no breakdown at all
        ]
    )

    assert account["by_model"]["opus"]["charged_usd"] == "0.1"
    assert account["by_model"]["haiku"]["charged_usd"] == UNKNOWN
    assert "the provider's charge for this model: not said" in shown(account)


def test_a_charge_nobody_reported_is_unknown_and_a_part_of_it_is_said_to_be_a_part() -> None:
    """3C-S3-1.2: nothing is a charge a provider can report. No report is not that."""
    none_reported = account_of([call(usage=[billed("opus")])])
    some_reported = account_of(
        [call(usage=[billed("opus")], charged="0.10"), call(usage=[billed("opus")])]
    )
    reported_as_nothing = account_of([call(usage=[billed("opus")], charged="0")])

    assert none_reported["charged_by_the_provider"]["usd"] == UNKNOWN
    assert "charged by the provider: unknown (it gave a cost for none of 1 call(s))" in shown(
        none_reported
    )
    assert some_reported["charged_by_the_provider"] == {
        "usd": "0.1",
        "calls_it_said_the_cost_of": 1,
        "calls_it_did_not": 1,
    }
    assert "0.1 USD known, for 1 of 2 call(s); the other 1 it gave no cost for" in shown(
        some_reported
    )
    assert reported_as_nothing["charged_by_the_provider"]["usd"] == "0"
    assert "charged by the provider: 0 USD over 1 call(s)" in shown(reported_as_nothing)


def test_a_launch_that_never_happened_is_said_in_text_as_it_is_in_json() -> None:
    """3C-S3-1.2: one call that never started does not read the same as no call at all."""
    never, nothing = account_of([call(state="not_started")]), account_of([])

    assert (never["calls"], never["calls_that_never_started"]) == (0, 1)
    assert "1 that never started" in shown(never)
    assert shown(never) != shown(nothing) and "never started" not in shown(nothing)
    assert nothing["charged_by_the_provider"]["usd"] == "0"  # no call, and nothing charged


def test_a_counter_that_was_not_reported_is_unknown_and_a_reported_zero_is_a_zero() -> None:
    account = account_of(
        [
            call(usage=[billed("opus", cache_read_tokens=None)]),
            call(usage=[billed("opus", cache_read_tokens=40, output_tokens=0)]),
            call(role="qa", state="errored"),  # ended, and said nothing of what it used
        ]
    )

    opus = account["by_model"]["opus"]
    assert opus["cache_read_tokens"] == UNKNOWN  # not 40: one call did not say
    assert (opus["input_tokens"], opus["output_tokens"]) == (20, 5)
    assert account["by_role"]["builder"]["cache_read_tokens"] == UNKNOWN
    assert account["by_role"]["builder"]["input_tokens"] == 20
    # A role one of whose calls said nothing has totals nobody can vouch for.
    assert account["by_role"]["qa"]["input_tokens"] == UNKNOWN
    assert account["by_role"]["qa"]["calls_with_no_use_reported"] == 1
    assert account["input_tokens"] == UNKNOWN and account["calls_with_no_use_reported"] == 1


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
    # Each said to be an estimate, with its table: for the whole, and for the role.
    assert shown(account).count("an estimate, price table") == 4


def test_calls_that_have_not_ended_or_never_started_are_counted_apart_and_add_nothing() -> None:
    account = account_of(
        [
            call(usage=[billed("opus")], charged="0.10"),
            call(state="intended"),
            call(state="uncertain"),
            call(state="not_started"),
        ]
    )

    assert account["calls"] == 1
    assert account["calls_in_flight"] == 1
    assert account["calls_whose_end_is_not_known"] == 1
    assert account["calls_that_never_started"] == 1
    assert account["charged_by_the_provider"]["usd"] == "0.1"
    assert account["by_role"]["builder"]["calls"] == 1
    assert "1 in flight" in shown(account) and "1 whose end is not known" in shown(account)


def test_a_models_name_as_the_provider_billed_it_cannot_drive_the_terminal() -> None:
    """3C-S3-1.8."""
    account = account_of([call(usage=[billed("opus\x1b[2J")], charged="0.1")])

    assert "\x1b" not in shown(account) and "opus\\x1b[2J" in shown(account)
    assert "opus\x1b[2J" in account["by_model"]  # whole, in the facts


def test_a_frozen_summary_keeps_its_estimates_by_the_table_that_made_them() -> None:
    """3C-S3-1.6: a feature whose calls span a change of prices. And a summary written
    before that was kept says its estimate's provenance is not on record."""
    frozen = totals_of(
        [
            call(
                role="builder",
                usage=[billed("opus")],
                charged="0.10",
                estimated="0.09",
                table="2026-10",
            ),
            call(
                role="builder",
                usage=[billed("opus")],
                charged="0.20",
                estimated="0.21",
                table="2026-11",
            ),
        ]
    )

    assert frozen["by_role"]["builder"]["estimated_by_table"] == [
        {"price_table": "2026-10", "usd": "0.09", "calls": 1},
        {"price_table": "2026-11", "usd": "0.21", "calls": 1},
    ]
    text = summary_text({"usage": frozen})
    assert "price table 2026-10): 0.09 USD" in text and "price table 2026-11): 0.21 USD" in text
    older = {"usage": {"calls_not_known": 0, "by_role": {"builder": {
        "calls": 2, "input_tokens": 20, "output_tokens": 10,
        "provider_cost_usd": "0.3", "estimated_cost_usd": "0.3",
    }}}}  # fmt: skip
    assert "which table, or tables, made it is not on record" in summary_text(older)
    assert "has not completed" in summary_text(None)
