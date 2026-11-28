"""The live account of what calls have used (3C-1.9, PO-8, PO-9): who spent it, and four
things kept apart that would say something untrue if added together."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from relay.application.final_checks import totals_of
from relay.application.usage import (
    NOT_ON_RECORD,
    UNKNOWN,
    account_of,
    account_text,
    frozen_account,
    summary_text,
)


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


def billed(
    model: str, cost: str | None = None, *, kept: bool = True, **counters: int | None
) -> dict[str, Any]:
    """A line of a call's record of use. `kept=False`: a record made before a model's
    charge was kept in it."""
    whole = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    return {"model": model, **whole, **counters, **({"provider_cost_usd": cost} if kept else {})}


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
        # No estimate was recorded for this model's part: said, and not made up now.
        "estimated_by_relay": [],
        "lines_with_no_estimate": 1,
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
                usage=[billed("opus", kept=False)],
                charged="0.10",
                billed_by_model={"opus": {"costUSD": 0.1}},
            ),
            call(usage=[billed("haiku", kept=False)], charged="0.01"),  # no breakdown at all
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


def test_a_model_charge_that_is_no_amount_is_unknown_and_breaks_nothing_else() -> None:
    """3C-S3-2.3: the provider sent `costUSD: true`. The record's reader refused it and
    kept null; the account does not go back to the raw report for it. And for a record
    made before the charge was kept, the raw report is read by the same rule."""
    account = account_of(
        [
            call(
                usage=[billed("opus")],  # kept, as null
                charged="0.10",
                billed_by_model={"opus": {"costUSD": True}},
            ),
            call(
                usage=[billed("haiku", kept=False)],
                charged="0.02",
                billed_by_model={"haiku": {"costUSD": True}},
            ),
            call(
                usage=[billed("sonnet", kept=False)],
                charged="0.03",
                billed_by_model={"sonnet": {"costUSD": "a lot"}},
            ),
            call(usage=[billed("sonnet", cost="not a number")], charged="0.01"),
            call(usage=[billed("fable", cost="NaN")], charged="0.00"),
            call(usage=[billed("fable", cost="Infinity")], charged="0.00"),
            call(usage=[billed("opus", cost="0.5")], role="qa", charged="0.5"),
        ]
    )

    assert account["by_model"]["opus"]["charged_usd"] == UNKNOWN  # one of two not said
    assert account["by_model"]["haiku"]["charged_usd"] == UNKNOWN
    assert account["by_model"]["sonnet"]["charged_usd"] == UNKNOWN
    assert account["by_model"]["fable"]["charged_usd"] == UNKNOWN  # not an amount either
    assert account["charged_by_the_provider"]["usd"] == "0.66"  # every whole call, readable
    assert account["by_role"]["qa"]["charged_by_the_provider"]["usd"] == "0.5"
    # A kept null is not overridden by a good amount in the raw report either.
    kept_null = account_of(
        [call(usage=[billed("opus")], charged="0.1", billed_by_model={"opus": {"costUSD": 0.1}})]
    )
    assert kept_null["by_model"]["opus"]["charged_usd"] == UNKNOWN


def test_a_role_whose_only_call_nobody_knows_the_end_of_has_an_account_of_that() -> None:
    """3C-S3-2.7."""
    account = account_of(
        [
            call(role="builder", usage=[billed("opus")], charged="0.10"),
            call(role="qa", state="uncertain"),
            call(role="sentinel", state="intended"),
            call(role="verifier", state="not_started"),
        ]
    )

    assert set(account["by_role"]) == {"builder", "qa", "sentinel"}  # not what never started
    qa, sentinel = account["by_role"]["qa"], account["by_role"]["sentinel"]
    assert (qa["calls"], qa["calls_whose_end_is_not_known"], qa["calls_in_flight"]) == (0, 1, 0)
    assert (sentinel["calls"], sentinel["calls_in_flight"]) == (0, 1)
    assert account["by_role"]["builder"]["calls_whose_end_is_not_known"] == 0
    assert account["charged_by_the_provider"]["usd"] == "0.1"  # ended calls' spend only
    text = shown(account)
    assert "the qa: 0 call(s), and 1 whose end is not known" in text
    assert "the sentinel: 0 call(s), and 1 in flight" in text


def test_a_frozen_account_says_the_same_in_text_and_in_json() -> None:
    """3C-S3-2.4: two tables and a call Relay could not price. Neither rendering has one
    sum of two tables, and both have the counters, the cache included."""
    summary = {
        "usage": totals_of(
            [
                call(
                    role="builder",
                    usage=[billed("opus", cache_read_tokens=9)],
                    charged="0.04",
                    estimated="0.04",
                    table="A",
                ),
                call(
                    role="builder",
                    usage=[billed("opus")],
                    charged="0.08",
                    estimated="0.08",
                    table="B",
                ),
                call(role="builder", usage=[billed("mystery")], charged="0.01"),
                call(role="qa", state="uncertain"),
            ]
        )
    }

    account = frozen_account(summary)
    text = summary_text(summary)

    builder = account["by_role"]["builder"]
    assert "estimated_cost_usd" not in builder  # no sum with no table to its name
    assert builder["estimated_by_relay"] == [
        {"price_table": "A", "usd": "0.04", "calls": 1},
        {"price_table": "B", "usd": "0.08", "calls": 1},
    ]
    assert (builder["calls"], builder["calls_with_no_estimate"]) == (3, 1)
    assert (builder["cache_read_tokens"], builder["cache_write_tokens"]) == (9, 0)
    assert builder["charged_by_the_provider_usd"] == "0.13"
    assert account["calls_not_known_then"] == 1
    assert "price table A): 0.04 USD over 1 call(s)" in text
    assert "price table B): 0.08 USD over 1 call(s)" in text
    assert "0.12" not in text and "calls Relay could not estimate: 1" in text
    assert "cache read 9, cache written 0" in text and "charged by the provider: 0.13" in text
    assert "calls whose use was not known then: 1" in text
    # What is stored is not changed by being shown.
    assert summary["usage"]["by_role"]["builder"]["estimated_cost_usd"] == "unknown"
    # A summary from before: its one sum, with its provenance said to be not on record.
    older = {"usage": {"calls_not_known": 0, "by_role": {"builder": {
        "calls": 2, "input_tokens": 20, "output_tokens": 10, "cache_read_tokens": 0,
        "cache_write_tokens": 0, "provider_cost_usd": "0.3", "estimated_cost_usd": "0.3",
    }}}}  # fmt: skip
    [estimate] = frozen_account(older)["by_role"]["builder"]["estimated_by_relay"]
    # 3C-S3-3.5: the writer of that time summed only when every call had an estimate, so
    # the sum covers both calls: the table is what is not on record.
    assert estimate == {"price_table": NOT_ON_RECORD, "usd": "0.3", "calls": 2}
    assert frozen_account(older)["by_role"]["builder"]["calls_with_no_estimate"] == 0
    assert "(an estimate): 0.3 over 2 call(s). This summary was" in summary_text(older)
    older["usage"]["by_role"]["builder"]["estimated_cost_usd"] = "unknown"
    [unsummed] = frozen_account(older)["by_role"]["builder"]["estimated_by_relay"]
    assert unsummed["calls"] == NOT_ON_RECORD
    assert frozen_account(older)["by_role"]["builder"]["calls_with_no_estimate"] == NOT_ON_RECORD
    # And a call that reported no use is said in the text as it is in the facts.
    silent = {"usage": totals_of([call(role="qa", state="errored")])}
    assert frozen_account(silent)["by_role"]["qa"]["calls_with_no_use_reported"] == 1
    assert "(1 call(s) reported no use)" in summary_text(silent)
    assert frozen_account({"usage": "unknown"}) == "unknown"


def test_each_models_estimate_is_shown_with_its_table_and_the_whole_call_is_counted_once() -> None:
    """3C-S3-3.3: one call billed to two models Relay has prices for."""
    account = account_of(
        [
            call(
                usage=[
                    {**billed("opus"), "estimated_cost_usd": "0.0004", "price_table": "2026-11"},
                    {**billed("haiku"), "estimated_cost_usd": "0.00008", "price_table": "2026-11"},
                ],
                charged="0.0005",
                estimated="0.00048",
            ),
            call(
                usage=[{**billed("opus"), "estimated_cost_usd": "0.1", "price_table": "2026-10"}],
                estimated="0.1",
                table="2026-10",
            ),
            call(usage=[billed("opus")], estimated="0.2"),  # recorded before this was kept
        ]
    )

    opus, haiku = account["by_model"]["opus"], account["by_model"]["haiku"]
    assert opus["estimated_by_relay"] == [
        {"price_table": "2026-10", "usd": "0.1", "lines": 1},
        {"price_table": "2026-11", "usd": "0.0004", "lines": 1},
    ]
    assert opus["lines_with_no_estimate"] == 1  # unknown, and not worked out again
    assert haiku["estimated_by_relay"] == [{"price_table": "2026-11", "usd": "0.00008", "lines": 1}]
    # The whole calls' estimates, once each, by table: not rebuilt from the models.
    assert account["estimated_by_relay"] == [
        {"price_table": "2026-10", "usd": "0.1", "calls": 1},
        {"price_table": "2026-11", "usd": "0.20048", "calls": 2},
    ]
    text = shown(account)
    assert (
        "estimated by Relay for this model (an estimate, price table 2026-11): 0.0004 USD" in text
    )
    assert "with no estimate on record for this model: 1" in text


def test_an_absurd_amount_is_shown_as_it_is_and_breaks_nothing() -> None:
    """3C-S3-3.6: the provider said a model's part cost ten to the twentieth dollars."""
    from relay.providers.stream import money_as_reported

    absurd = money_as_reported(1e20)
    assert absurd is not None
    assert money_as_reported(float("inf")) is None and money_as_reported(float("nan")) is None
    calls = [
        call(usage=[billed("opus", cost=str(absurd))], charged="0.10"),
        call(
            usage=[billed("haiku", cost="0.01")],
            role="qa",
            charged=str(absurd),
            estimated=str(absurd),
        ),
    ]

    account = account_of(calls)
    frozen = totals_of(calls)

    assert account["by_model"]["opus"]["charged_usd"] == "100000000000000000000"
    assert account["by_model"]["haiku"]["charged_usd"] == "0.01"
    assert account["charged_by_the_provider"]["usd"] == "100000000000000000000.1"
    assert account["by_role"]["builder"]["charged_by_the_provider"]["usd"] == "0.1"
    assert frozen["by_role"]["qa"]["provider_cost_usd"] == "100000000000000000000"
    assert "100000000000000000000.1 USD" in shown(account)
