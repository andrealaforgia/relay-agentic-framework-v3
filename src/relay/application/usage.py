"""What calls have used, as it is known now: the live account.

Read from the record of each call, not from a feature's summary. A summary is written
once, when a feature completes, and says what was known then; this says what is known
at the moment it is asked, a call that reported late included.

Four things are kept apart, because adding them together would say something untrue:

- A call is counted once, however many models it was billed to.
- Each model's tokens are that model's. A counter a call did not report for a model
  leaves that model's total of it unknown: not zero, and not the sum of the rest.
- What the provider said a call cost is the call's, whole. It is not shared out among
  the models the call was billed to unless the provider shared it out.
- What Relay estimates is an estimate, by a table of prices that has a version. Estimates
  made by different tables are not added to each other.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from relay.persistence.database import Pool

COUNTED = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
UNKNOWN = "unknown"


def _usd(amount: Decimal) -> str:
    # To a hundred-millionth of a dollar: what a provider reports as a binary fraction is
    # not shown with the noise of one.
    return f"{amount.quantize(Decimal('0.00000001')).normalize():f}"


def account_of(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The account of some calls. Each has its role, its state, what it reported using by
    model billed, its two costs, and the price table its estimate was made by."""
    made = [each for each in calls if each["state"] != "not_started"]
    ended = [each for each in made if each["state"] not in ("intended", "uncertain")]
    by_role: dict[str, int] = {}
    by_model: dict[str, dict[str, Any]] = {}
    parts: dict[str, dict[str, list[Any]]] = {}
    no_use = 0
    for call in ended:
        by_role[call["role"]] = by_role.get(call["role"], 0) + 1
        billed = [each for each in call["usage"] or () if isinstance(each, Mapping)]
        if not any(each.get(counted) is not None for each in billed for counted in COUNTED):
            # Nothing of what it used: an empty report is no report, and a zero is one.
            no_use += 1
        for model in {str(each.get("model") or UNKNOWN) for each in billed}:
            # Once for a call, however many lines of its report name the model.
            by_model.setdefault(model, {"calls": 0})["calls"] += 1
        for each in billed:
            into = parts.setdefault(
                str(each.get("model") or UNKNOWN), {counted: [] for counted in COUNTED}
            )
            for counted in COUNTED:
                into[counted].append(each.get(counted))
    for model, counters in parts.items():
        for counted in COUNTED:
            reported = counters[counted]
            by_model[model][counted] = UNKNOWN if None in reported else sum(reported)
    charged = [each["provider_cost_usd"] for each in ended if each["provider_cost_usd"] is not None]
    by_table: dict[str, list[Decimal]] = {}
    no_estimate = 0
    for call in ended:
        if call["estimated_cost_usd"] is None:
            no_estimate += 1
        else:
            by_table.setdefault(str(call["price_table"] or UNKNOWN), []).append(
                Decimal(call["estimated_cost_usd"])
            )
    return {
        # Counted once each, whatever they were billed to.
        "calls": len(ended),
        "calls_in_flight": sum(1 for each in made if each["state"] == "intended"),
        "calls_whose_end_is_not_known": sum(1 for each in made if each["state"] == "uncertain"),
        "calls_that_never_started": len(calls) - len(made),
        "calls_with_no_use_reported": no_use,
        "by_role": dict(sorted(by_role.items())),
        "by_model": dict(sorted(by_model.items())),
        "charged_by_the_provider": {
            "usd": _usd(sum((Decimal(each) for each in charged), Decimal(0))),
            "calls_it_said_the_cost_of": len(charged),
            "calls_it_did_not": len(ended) - len(charged),
        },
        # Relay's own, each by the table of prices that made it.
        "estimated_by_relay": [
            {"price_table": table, "usd": _usd(sum(amounts, Decimal(0))), "calls": len(amounts)}
            for table, amounts in sorted(by_table.items())
        ],
        "calls_with_no_estimate": no_estimate,
    }


async def live_usage(pool: Pool, project_id: str, feature_id: str | None = None) -> dict[str, Any]:
    """The account of a project's calls, or of one feature's, as it is known now."""
    async with pool.connection() as connection:
        calls = await (
            await connection.execute(
                "SELECT v.role, v.state, v.usage, v.provider_cost_usd, v.estimated_cost_usd, "
                "v.price_table FROM invocations v JOIN assignments a USING (assignment_id) "
                "WHERE a.project_id = %s AND (%s::text IS NULL OR a.feature_id = %s) "
                "ORDER BY v.started_at",
                (project_id, feature_id, feature_id),
            )
        ).fetchall()
    return {"project": project_id, "feature": feature_id, **account_of(calls)}


def account_text(account: Mapping[str, Any]) -> str:
    """The same facts, for a person: every estimate labelled an estimate, with its table."""
    of = (
        f"feature {account['feature']} of {account['project']}"
        if account.get("feature")
        else (f"project {account['project']}")
    )
    lines = [
        f"what calls have used, as known now: {of}",
        f"  calls made: {account['calls']}"
        + (f", and {account['calls_in_flight']} in flight" if account["calls_in_flight"] else "")
        + (
            f", and {account['calls_whose_end_is_not_known']} whose end is not known"
            if account["calls_whose_end_is_not_known"]
            else ""
        ),
    ]
    if account["calls_with_no_use_reported"]:
        lines.append(f"  calls that reported no use: {account['calls_with_no_use_reported']}")
    if account["by_role"]:
        lines.append(
            "  by role: " + ", ".join(f"{role} {n}" for role, n in account["by_role"].items())
        )
    for model, used in account["by_model"].items():
        lines.append(
            f"  {model}: {used['calls']} call(s), in {used['input_tokens']}, out "
            f"{used['output_tokens']}, cache read {used['cache_read_tokens']}, cache written "
            f"{used['cache_write_tokens']}"
        )
    charged = account["charged_by_the_provider"]
    lines.append(
        f"  charged by the provider: {charged['usd']} USD over "
        f"{charged['calls_it_said_the_cost_of']} call(s)"
        + (
            f"; {charged['calls_it_did_not']} call(s) it gave no cost for"
            if charged["calls_it_did_not"]
            else ""
        )
    )
    for estimate in account["estimated_by_relay"]:
        lines.append(
            f"  estimated by Relay (an estimate, price table {estimate['price_table']}): "
            f"{estimate['usd']} USD over {estimate['calls']} call(s)"
        )
    if account["calls_with_no_estimate"]:
        lines.append(f"  calls Relay could not estimate: {account['calls_with_no_estimate']}")
    return "\n".join(lines)
