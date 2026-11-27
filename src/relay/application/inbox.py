"""Everything waiting for the Product Owner, in one list.

Five kinds of thing, each with what there is to see and what can be done about it:

    gate           a roadmap, a story or a replacement put to the person
    final_check    a feature's final check that failed on trunk
    question       something a role asked before going on
    parked_call    work stopped on a call a person has to say something about
    refused_push   an integration the repository refused

Each item has an identity, which is the identity of the record it is about, and a
`basis`: what the person was shown of it. A decision names the item and, where the
service can hold it to that, the basis; an item that has been closed since, or is no
longer what was shown, is not decided on.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from relay.persistence.database import Connection, Pool

GATE, FINAL_CHECK, QUESTION, PARKED_CALL, REFUSED_PUSH = (
    "gate",
    "final_check",
    "question",
    "parked_call",
    "refused_push",
)

# What can be done about each kind. `reject` is a story gate's alone.
ACTIONS: dict[str, tuple[str, ...]] = {
    GATE: ("approve", "request-changes"),
    FINAL_CHECK: ("retry", "request-changes"),
    QUESTION: ("answer",),
    PARKED_CALL: ("retry", "set-aside"),
    REFUSED_PUSH: ("retry",),
}


@dataclass(frozen=True)
class Item:
    item_id: str
    kind: str
    # What it is about, in a line.
    about: str
    feature_id: str | None
    # What there is to see before deciding.
    evidence: dict[str, Any]
    actions: tuple[str, ...]
    # A digest of what was shown: an item decided on is this item, as it was shown.
    basis: str
    waiting_since: str
    # The shortest start of its identity that is this item's alone among what is waiting:
    # what a person is shown to type.
    handle: str = ""


def _text(recorded: Any, most: int = 2000) -> str:
    """What a run printed, as text a person can read, cut where it is very long."""
    text = bytes(recorded or b"").decode(errors="replace")
    return text if len(text) <= most else text[:most] + f"... ({len(text) - most} more characters)"


def _handle(item_id: str, among: list[Item]) -> str:
    """The shortest start of an identity, of eight characters or more, that no other item
    waiting begins with. Identities made in the same moment begin alike."""
    for length in range(8, len(item_id) + 1):
        start = item_id[:length]
        if sum(1 for other in among if other.item_id.startswith(start)) == 1:
            return start
    return item_id


def _basis(of: Any) -> str:
    canonical = json.dumps(of, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


async def parked_evidence(connection: Connection, invocation_id: Any) -> dict[str, Any] | None:
    """What a person is shown of work stopped on a call: the work, how many calls were
    really launched for it, and what they cost, with what is known kept apart from what
    is estimated and from what is not known at all. None if no such call is parked."""
    call = await (
        await connection.execute(
            "SELECT v.role, v.state, v.reason, a.assignment_id, a.kind, a.feature_id, "
            "a.story_id, a.behaviour_id, a.parked FROM invocations v JOIN assignments a "
            "USING (assignment_id) WHERE v.invocation_id = %s",
            (invocation_id,),
        )
    ).fetchone()
    if call is None:
        return None
    account = await (
        await connection.execute(
            # A call that never started was not made, and cost nothing. A charge the
            # provider reported is a charge; an estimate is Relay's own and is said to
            # be one; a call launched with neither is not known, and is counted so.
            "SELECT count(*) FILTER (WHERE state <> 'not_started') AS launched, "
            "count(*) FILTER (WHERE state = 'not_started') AS never_started, "
            "sum(provider_cost_usd) AS charged, "
            "sum(estimated_cost_usd) FILTER (WHERE provider_cost_usd IS NULL) AS estimated, "
            "count(*) FILTER (WHERE state <> 'not_started' AND provider_cost_usd IS NULL "
            "AND estimated_cost_usd IS NULL) AS unknown FROM invocations WHERE assignment_id = %s",
            (call["assignment_id"],),
        )
    ).fetchone()
    assert account is not None
    subject = call["behaviour_id"] or call["story_id"] or call["feature_id"]
    return {
        "work": f"{call['kind']} of {subject}",
        "role": call["role"],
        "feature": call["feature_id"],
        "calls_launched": int(account["launched"]),
        "calls_that_never_started": int(account["never_started"]),
        "why_parked": call["parked"],
        "how_the_call_ended": call["state"],
        "what_it_said": call["reason"],
        "charged_usd": str(account["charged"]) if account["charged"] is not None else "0",
        "estimated_usd_where_no_charge_was_reported": (
            str(account["estimated"]) if account["estimated"] is not None else "0"
        ),
        "calls_whose_cost_is_not_known": int(account["unknown"]),
    }


def parked_basis(invocation_id: Any, evidence: dict[str, Any]) -> str:
    """What a decision about a parked call is held to: the call, and all that was shown."""
    return _basis({"call": str(invocation_id), **evidence})


class Inbox:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def item(self, project_id: str, item_id: str) -> Item | None:
        """One open item, by the whole of its identity or by the start of it, if that is
        the start of one item's only."""
        matching = [
            each for each in await self.items(project_id) if each.item_id.startswith(item_id)
        ]
        exact = [each for each in matching if each.item_id == item_id]
        if exact:
            return exact[0]
        return matching[0] if len(matching) == 1 and len(item_id) >= 6 else None

    async def items(self, project_id: str) -> list[Item]:
        """What is waiting for the Product Owner in a project, longest-waiting first."""
        found: list[Item] = []
        async with self._pool.connection() as connection:
            gates = await (
                await connection.execute(
                    "SELECT g.gate_id, g.kind, g.feature_id, g.story_id, g.manifest, "
                    "g.manifest_digest, i.opened_at FROM inbox_items i JOIN gates g USING (gate_id) "
                    "WHERE i.project_id = %s AND i.state = 'open' AND g.state = 'open'",
                    (project_id,),
                )
            ).fetchall()
            for gate in gates:
                observed: dict[str, Any] = {}
                if gate["kind"] == "completion":
                    kind, about = FINAL_CHECK, f"the final check of {gate['feature_id']} failed"
                    actions = ACTIONS[FINAL_CHECK]
                    # What each check was to produce and what the oracle saw it produce:
                    # the two things a person weighs between checking again and changing
                    # the roadmap.
                    runs = await (
                        await connection.execute(
                            "SELECT o.name, o.contract, r.satisfied, e.exit_code, e.stdout, "
                            "e.stderr FROM completion_receipts r JOIN obligations o "
                            "USING (obligation_id) JOIN executions e USING (execution_id) "
                            "WHERE r.batch_id = %s ORDER BY o.name",
                            (gate["manifest"].get("batch_id"),),
                        )
                    ).fetchall()
                    observed = {
                        "observed": [
                            {
                                "check": run["name"],
                                "satisfied": run["satisfied"],
                                "expected": run["contract"].get("expect"),
                                "ran": run["contract"].get("launch"),
                                "exit_code": run["exit_code"],
                                "stdout": _text(run["stdout"]),
                                "stderr": _text(run["stderr"]),
                            }
                            for run in runs
                        ]
                    }
                else:
                    kind = GATE
                    about = {
                        "roadmap": f"the roadmap of {gate['feature_id']}",
                        "story": f"story {gate['story_id']} of {gate['feature_id']}",
                        "supersession": f"a replacement in {gate['feature_id']}",
                    }[gate["kind"]]
                    actions = ACTIONS[GATE] + (("reject",) if gate["kind"] == "story" else ())
                found.append(
                    Item(
                        str(gate["gate_id"]),
                        kind,
                        about,
                        gate["feature_id"],
                        {
                            "gate": gate["kind"],
                            "story": gate["story_id"],
                            **gate["manifest"],
                            **observed,
                        },
                        actions,
                        str(gate["manifest_digest"]),
                        gate["opened_at"].isoformat(timespec="seconds"),
                    )
                )
            questions = await (
                await connection.execute(
                    "SELECT q.question_id, q.feature_id, q.scope_story, q.asked_by, q.text, "
                    "q.why, q.options, i.opened_at FROM inbox_items i JOIN questions q "
                    "USING (question_id) WHERE i.project_id = %s AND i.state = 'open' "
                    "AND q.state = 'open'",
                    (project_id,),
                )
            ).fetchall()
            for question in questions:
                evidence = {
                    "text": question["text"],
                    "why": question["why"],
                    "options": list(question["options"]),
                    "asked_by": question["asked_by"],
                    "story": question["scope_story"] or None,
                }
                found.append(
                    Item(
                        str(question["question_id"]),
                        QUESTION,
                        f"a question from the {question['asked_by']} in {question['feature_id']}",
                        question["feature_id"],
                        evidence,
                        ACTIONS[QUESTION],
                        _basis({"question": str(question["question_id"]), **evidence}),
                        question["opened_at"].isoformat(timespec="seconds"),
                    )
                )
            parked = await (
                await connection.execute(
                    "SELECT v.invocation_id, i.opened_at FROM inbox_items i JOIN invocations v "
                    "USING (invocation_id) JOIN assignments a USING (assignment_id) "
                    "WHERE i.project_id = %s AND i.state = 'open' AND i.kind = 'invocation' "
                    "AND a.state IN ('wanted', 'granted')",
                    (project_id,),
                )
            ).fetchall()
            for call in parked:
                about_call = await parked_evidence(connection, call["invocation_id"])
                assert about_call is not None
                found.append(
                    Item(
                        str(call["invocation_id"]),
                        PARKED_CALL,
                        f"the {about_call['role']}'s {about_call['work']} is stopped",
                        about_call["feature"],
                        about_call,
                        ACTIONS[PARKED_CALL],
                        parked_basis(call["invocation_id"], about_call),
                        call["opened_at"].isoformat(timespec="seconds"),
                    )
                )
            refused = await (
                await connection.execute(
                    # A behaviour's latest intent, refused, with the behaviour blocked on
                    # it: once a person has allowed a retry the behaviour is no longer
                    # blocked, and a later refusal is another intent and another item.
                    "SELECT i.intent_id, i.behaviour_id, i.candidate_revision, i.expected_trunk, "
                    "i.trunk, i.reason, i.created_at, i.sends_started FROM integration_intents i "
                    "JOIN behaviours b USING (project_id, behaviour_id) "
                    "WHERE i.project_id = %s AND i.state = 'rejected' AND b.state = 'blocked' "
                    "AND i.created_at = (SELECT max(x.created_at) FROM integration_intents x "
                    "WHERE x.project_id = i.project_id AND x.behaviour_id = i.behaviour_id)",
                    (project_id,),
                )
            ).fetchall()
            for push in refused:
                evidence = {
                    "behaviour": push["behaviour_id"],
                    "candidate": push["candidate_revision"],
                    "onto": push["expected_trunk"],
                    "branch": push["trunk"],
                    "what_the_repository_said": push["reason"],
                    "sends_made": int(push["sends_started"]),
                }
                found.append(
                    Item(
                        str(push["intent_id"]),
                        REFUSED_PUSH,
                        f"the repository refused {push['behaviour_id']} onto {push['trunk']}",
                        None,
                        evidence,
                        ACTIONS[REFUSED_PUSH],
                        _basis({"intent": str(push["intent_id"]), **evidence}),
                        push["created_at"].isoformat(timespec="seconds"),
                    )
                )
        return [
            dataclasses.replace(each, handle=_handle(each.item_id, found))
            for each in sorted(found, key=lambda each: (each.waiting_since, each.item_id))
        ]
