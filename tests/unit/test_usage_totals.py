"""What a feature's summary says its calls used. A total is a total only if nothing that
goes into it is missing; what was not reported is unknown, and a reported zero is a zero."""

from decimal import Decimal
from typing import Any

from relay.application.final_checks import totals_of


def call(role: str, *billed: dict[str, Any], state: str = "applied", cost: Any = "0.01") -> Any:
    return {
        "role": role,
        "state": state,
        "usage": list(billed) or None,
        "provider_cost_usd": Decimal(cost) if cost is not None else None,
        "estimated_cost_usd": Decimal(cost) if cost is not None else None,
    }


WHOLE = {"input_tokens": 2, "output_tokens": 7, "cache_read_tokens": 0, "cache_write_tokens": 0}


def test_a_feature_with_no_recorded_call_says_only_that_its_use_is_unknown() -> None:
    assert totals_of([]) == "unknown"


def test_a_counter_one_model_of_a_call_did_not_report_is_not_a_total() -> None:
    """3B-S4-1.3: two models billed by one call; one says nothing of what it wrote."""
    partly = [call("builder", {"input_tokens": 2}, {"input_tokens": 3, "output_tokens": 7})]

    builder = totals_of(partly)["by_role"]["builder"]

    assert builder["input_tokens"] == 5
    assert builder["output_tokens"] == "unknown"
    assert (builder["cache_read_tokens"], builder["cache_write_tokens"]) == ("unknown", "unknown")


def test_one_call_that_reported_nothing_makes_every_total_of_its_role_unknown() -> None:
    """3B-S4-1.3: one reported call and one that was not."""
    qa = totals_of([call("qa", WHOLE), call("qa", cost=None)])["by_role"]["qa"]

    assert (qa["calls"], qa["calls_with_no_use_reported"]) == (2, 1)
    assert [qa[name] for name in WHOLE] == ["unknown"] * 4
    assert (qa["provider_cost_usd"], qa["estimated_cost_usd"]) == ("unknown", "unknown")


def test_a_zero_that_was_reported_is_a_zero_and_everything_reported_is_summed() -> None:
    account = totals_of(
        [call("qa", WHOLE), call("qa", WHOLE), call("qa", state="uncertain", cost=None)]
    )

    assert account["calls_not_known"] == 1
    assert account["by_role"]["qa"] == {
        "calls": 2,
        "calls_with_no_use_reported": 0,
        "input_tokens": 4,
        "output_tokens": 14,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "provider_cost_usd": "0.02",
        "estimated_cost_usd": "0.02",
    }


def test_a_report_with_no_counter_in_it_is_no_report_of_use() -> None:
    """3B-S4-2.6: the provider's event had `usage: {}`. One model billed, nothing said."""
    empty = {name: None for name in WHOLE}
    zero = dict.fromkeys(WHOLE, 0)

    said_nothing = totals_of([call("qa", empty)])["by_role"]["qa"]
    said_zero = totals_of([call("qa", zero)])["by_role"]["qa"]

    assert said_nothing["calls_with_no_use_reported"] == 1
    assert said_nothing["input_tokens"] == "unknown"
    assert (said_zero["calls_with_no_use_reported"], said_zero["input_tokens"]) == (0, 0)
