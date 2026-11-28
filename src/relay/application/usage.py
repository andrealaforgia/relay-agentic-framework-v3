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


def _charged(ended: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What the provider said these calls cost. Unknown, if it said so for none of them:
    a charge of nothing is something a provider can report, and is not the same thing.
    A sum, if for all. If for some, the sum of those, said to be of those."""
    charged = [each["provider_cost_usd"] for each in ended if each["provider_cost_usd"] is not None]
    return {
        "usd": (
            UNKNOWN
            if ended and not charged
            else _usd(sum((Decimal(each) for each in charged), Decimal(0)))
        ),
        "calls_it_said_the_cost_of": len(charged),
        "calls_it_did_not": len(ended) - len(charged),
    }


def _totals(ended: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What some ended calls used and cost, each call counted once."""
    lines = [each for call in ended for each in call["usage"] or () if isinstance(each, Mapping)]
    silent = sum(
        1
        for call in ended
        if not any(
            each.get(counted) is not None
            for each in call["usage"] or ()
            if isinstance(each, Mapping)
            for counted in COUNTED
        )
    )
    by_table: dict[str, list[Decimal]] = {}
    for call in ended:
        if call["estimated_cost_usd"] is not None:
            by_table.setdefault(str(call["price_table"] or UNKNOWN), []).append(
                Decimal(call["estimated_cost_usd"])
            )
    return {
        "calls": len(ended),
        # Nothing of what it used: an empty report is no report, and a zero is one.
        "calls_with_no_use_reported": silent,
        # A total only if nothing that goes into it is missing: every call said what it
        # used, and every line of what it said has the counter.
        **{
            counted: (
                UNKNOWN
                if silent or any(each.get(counted) is None for each in lines)
                else sum(each[counted] for each in lines)
            )
            for counted in COUNTED
        },
        "charged_by_the_provider": _charged(ended),
        # Relay's own, each by the table of prices that made it.
        "estimated_by_relay": [
            {"price_table": table, "usd": _usd(sum(amounts, Decimal(0))), "calls": len(amounts)}
            for table, amounts in sorted(by_table.items())
        ],
        "calls_with_no_estimate": sum(1 for each in ended if each["estimated_cost_usd"] is None),
    }


def account_of(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The account of some calls. Each has its role, its state, what it reported using by
    model billed, its two costs, the price table its estimate was made by, and what the
    provider's own breakdown by model said, where the call's record of use predates
    keeping that."""
    made = [each for each in calls if each["state"] != "not_started"]
    ended = [each for each in made if each["state"] not in ("intended", "uncertain")]
    by_model: dict[str, dict[str, Any]] = {}
    parts: dict[str, dict[str, list[Any]]] = {}
    for call in ended:
        billed = [each for each in call["usage"] or () if isinstance(each, Mapping)]
        breakdown = call.get("billed_by_model")
        breakdown = breakdown if isinstance(breakdown, Mapping) else {}
        for model in {str(each.get("model") or UNKNOWN) for each in billed}:
            # Once for a call, however many lines of its report name the model.
            by_model.setdefault(model, {"calls": 0})["calls"] += 1
        for each in billed:
            model = str(each.get("model") or UNKNOWN)
            into = parts.setdefault(model, {name: [] for name in (*COUNTED, "charged")})
            for counted in COUNTED:
                into[counted].append(each.get(counted))
            said = each.get("provider_cost_usd")
            if said is None and isinstance(breakdown.get(model), Mapping):
                said = breakdown[model].get("costUSD")
            into["charged"].append(said)
    for model, counters in parts.items():
        for counted in COUNTED:
            reported = counters[counted]
            by_model[model][counted] = UNKNOWN if None in reported else sum(reported)
        # What the provider attributed to this model, where it attributed anything: a
        # sum only if it did for every line billed to the model.
        charges = counters["charged"]
        by_model[model]["charged_usd"] = (
            UNKNOWN
            if None in charges
            else _usd(sum((Decimal(str(each)) for each in charges), Decimal(0)))
        )
    roles = sorted({str(each["role"]) for each in ended})
    return {
        # Counted once each, whatever they were billed to; the provider's charge for a
        # whole call is in this total once, and is not the sum of what is under models.
        **_totals(ended),
        "calls_in_flight": sum(1 for each in made if each["state"] == "intended"),
        "calls_whose_end_is_not_known": sum(1 for each in made if each["state"] == "uncertain"),
        "calls_that_never_started": len(calls) - len(made),
        # Who spent it (PO-8): each role's own calls, tokens, charges and estimates.
        "by_role": {
            role: _totals([each for each in ended if each["role"] == role]) for role in roles
        },
        "by_model": dict(sorted(by_model.items())),
    }


class NoSuchFeature(LookupError):
    """The feature named is not one of the project's."""


async def live_usage(pool: Pool, project_id: str, feature_id: str | None = None) -> dict[str, Any]:
    """The account of a project's calls, or of one feature's, as it is known now. A
    feature that has made no call yet has an account of nothing; a name that is no
    feature of the project has none at all."""
    async with pool.connection() as connection:
        if feature_id is not None:
            there = await (
                await connection.execute(
                    "SELECT 1 FROM features WHERE project_id = %s AND feature_id = %s",
                    (project_id, feature_id),
                )
            ).fetchone()
            if there is None:
                raise NoSuchFeature(f"there is no feature {feature_id!r} in {project_id}")
        calls = await (
            await connection.execute(
                "SELECT v.role, v.state, v.usage, v.provider_cost_usd, v.estimated_cost_usd, "
                "v.price_table, v.envelope->'modelUsage' AS billed_by_model "
                "FROM invocations v JOIN assignments a USING (assignment_id) "
                "WHERE a.project_id = %s AND (%s::text IS NULL OR a.feature_id = %s) "
                "ORDER BY v.started_at",
                (project_id, feature_id, feature_id),
            )
        ).fetchall()
    return {"project": project_id, "feature": feature_id, **account_of(calls)}


def _charged_text(charged: Mapping[str, Any]) -> str:
    said, silent = charged["calls_it_said_the_cost_of"], charged["calls_it_did_not"]
    if charged["usd"] == UNKNOWN:
        return f"unknown (it gave a cost for none of {silent} call(s))"
    if silent:
        return (
            f"{charged['usd']} USD known, for {said} of {said + silent} call(s); the other "
            f"{silent} it gave no cost for"
        )
    return f"{charged['usd']} USD over {said} call(s)"


def _totals_text(totals: Mapping[str, Any], indent: str) -> list[str]:
    lines = [
        f"{indent}tokens: in {totals['input_tokens']}, out {totals['output_tokens']}, cache "
        f"read {totals['cache_read_tokens']}, cache written {totals['cache_write_tokens']}"
        + (
            f"  ({totals['calls_with_no_use_reported']} call(s) reported no use)"
            if totals["calls_with_no_use_reported"]
            else ""
        ),
        f"{indent}charged by the provider: {_charged_text(totals['charged_by_the_provider'])}",
    ]
    for estimate in totals["estimated_by_relay"]:
        lines.append(
            f"{indent}estimated by Relay (an estimate, price table "
            f"{_shown(estimate['price_table'])}): {estimate['usd']} USD over "
            f"{estimate['calls']} call(s)"
        )
    if totals["calls_with_no_estimate"]:
        lines.append(f"{indent}calls Relay could not estimate: {totals['calls_with_no_estimate']}")
    return lines


def _shown(text: object) -> str:
    """A name that came from outside (a model's, as the provider billed it), made safe to
    put on a terminal."""
    from relay.cli.terminal import visible

    return visible(str(text))


def account_text(account: Mapping[str, Any]) -> str:
    """The same facts, for a person: every estimate labelled an estimate, with its table,
    and whatever is not known said to be unknown."""
    of = (
        f"feature {account['feature']} of {account['project']}"
        if account.get("feature")
        else f"project {account['project']}"
    )
    lines = [
        f"what calls have used, as known now: {of}",
        f"  calls made: {account['calls']}"
        + (f", and {account['calls_in_flight']} in flight" if account["calls_in_flight"] else "")
        + (
            f", and {account['calls_whose_end_is_not_known']} whose end is not known"
            if account["calls_whose_end_is_not_known"]
            else ""
        )
        + (
            f", and {account['calls_that_never_started']} that never started"
            if account["calls_that_never_started"]
            else ""
        ),
    ]
    if account["calls"]:
        lines += _totals_text(account, "  ")
    for role, totals in account["by_role"].items():
        lines.append(f"  the {_shown(role)}: {totals['calls']} call(s)")
        lines += _totals_text(totals, "    ")
    for model, used in account["by_model"].items():
        lines.append(
            f"  on {_shown(model)}: {used['calls']} call(s), in {used['input_tokens']}, out "
            f"{used['output_tokens']}, cache read {used['cache_read_tokens']}, cache written "
            f"{used['cache_write_tokens']}; the provider's charge for this model: "
            + ("not said" if used["charged_usd"] == UNKNOWN else f"{used['charged_usd']} USD")
        )
    return "\n".join(lines)


async def summary_of(pool: Pool, project_id: str, feature_id: str) -> dict[str, Any] | None:
    """What a completed feature's summary recorded when it completed; None until then."""
    async with pool.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT summary FROM feature_summaries WHERE project_id = %s AND feature_id = %s",
                (project_id, feature_id),
            )
        ).fetchone()
    return dict(row["summary"]) if row else None


def summary_text(frozen: Mapping[str, Any] | None) -> str:
    """A frozen summary's account of use, for a person. Its estimates are shown by the
    table that made them where the summary kept that; one written before that was kept
    says so, and its estimate is not passed off as being of one table."""
    if frozen is None:
        return "the feature has not completed: it has no summary yet"
    usage = frozen.get("usage")
    if not isinstance(usage, Mapping):
        return f"as recorded when the feature completed: {usage}"
    lines = [
        "as recorded when the feature completed (never changed afterwards):",
        f"  calls whose use was not known then: {usage.get('calls_not_known', 0)}",
    ]
    for role, totals in dict(usage.get("by_role", {})).items():
        lines.append(
            f"  the {_shown(role)}: {totals['calls']} call(s), in {totals['input_tokens']}, out "
            f"{totals['output_tokens']}; charged {totals['provider_cost_usd']}"
        )
        tables = totals.get("estimated_by_table")
        if tables is None:
            lines.append(
                f"    estimated by Relay (an estimate): {totals['estimated_cost_usd']}. This "
                "summary was written before estimates were kept by price table: which table, "
                "or tables, made it is not on record"
            )
        for each in tables or ():
            lines.append(
                f"    estimated by Relay (an estimate, price table "
                f"{_shown(each['price_table'])}): {each['usd']} USD over {each['calls']} call(s)"
            )
    return "\n".join(lines)
