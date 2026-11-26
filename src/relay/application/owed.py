"""What a story needs next, derived from where it stands.

Each transition owes what follows it, in its own transaction. Twice a transition was
found with nothing to carry it on, and a list of triggers will keep having holes. So one
rule says what a story that is being worked on needs, and it is asked in the
transactions that move a story or one of its behaviours, and by the pass. It owes only
what is not already owed: it is the net under the triggers, and the definition beside
them.
"""

from __future__ import annotations

import uuid
from typing import Any

from psycopg import errors

from relay.application.assignments import ASSESS, BUILD, RED, SEAL, SPECIFY, owe
from relay.persistence.database import Connection
from relay.transport.envelope import Envelope, MessageKind

CONTROL_PLANE = "relay/control-plane"
_ON_TRUNK = ("integrated", "completed")


async def owe_what_is_needed(connection: Connection, project_id: str) -> None:
    """For every story of the project that is being worked on. Whether the work can
    start now is not asked: a pause stops it being claimed, not being owed."""
    stories = await (
        await connection.execute(
            "SELECT s.*, story_basis(s.project_id, s.story_id) AS basis, "
            "not_current(s.project_id, s.story_id, NULL) AS not_current, "
            "EXISTS (SELECT 1 FROM questions q WHERE q.project_id = s.project_id "
            "AND q.feature_id = s.feature_id AND q.state = 'open' "
            "AND q.scope_story IN ('', s.story_id)) AS asked, "
            # Of the story or of any behaviour of it: `not_current` asked of the story
            # alone does not see a contract its Verifier has yet to answer for.
            "EXISTS (SELECT 1 FROM assignments a WHERE a.project_id = s.project_id "
            "AND a.kind = 'reconfirm' AND a.state IN ('wanted', 'granted') "
            "AND a.story_id = s.story_id) AS reconfirming "
            "FROM stories s WHERE s.project_id = %s "
            "AND s.state IN ('released', 'changes_requested') ORDER BY s.story_id",
            (project_id,),
        )
    ).fetchall()
    for story in stories:
        # A question's answer owes what asked it, and a reconfirmation's completion asks
        # this again: until then the story's next step is not known.
        if story["asked"] or story["not_current"] is not None or story["reconfirming"]:
            continue
        await _owe_for(connection, story)


async def owe_red(connection: Connection, project_id: str, behaviour_id: str) -> None:
    """One red per behaviour and red cycle, the control plane's to observe: owed in the
    transaction that registers, rewrites or amends a contract, or reopens a red. A red
    that showed the contract cannot serve stays on record as that cycle's, so the
    behaviour, which still awaits a red, is not owed the same one again: the Verifier is
    owed the contract, and the contract written again is a new cycle."""
    behaviour = await (
        await connection.execute(
            "SELECT state, red_cycle, correlation_id, story_id FROM behaviours "
            "WHERE project_id = %s AND behaviour_id = %s",
            (project_id, behaviour_id),
        )
    ).fetchone()
    if behaviour is None or behaviour["state"] != "validating_red":
        return
    asked_for = await (
        await connection.execute(
            "SELECT 1 FROM assignments WHERE project_id = %s AND behaviour_id = %s "
            "AND kind = 'red' AND state <> 'retired' AND issued_on->>'red_cycle' = %s",
            (project_id, behaviour_id, str(behaviour["red_cycle"])),
        )
    ).fetchone()
    if asked_for is not None:
        # Owed, being observed, or observed and found wanting: once for this cycle.
        return
    await _owe(
        connection,
        RED,
        Envelope(
            message_kind=MessageKind.COMMAND,
            event_type="red.requested",
            project_id=project_id,
            behaviour_id=behaviour_id,
            sender=CONTROL_PLANE,
            recipient="integrator",
            idempotency_key=f"red:{project_id}:{behaviour_id}:{behaviour['red_cycle']}",
            correlation_id=behaviour["correlation_id"],
            payload={"red_cycle": behaviour["red_cycle"]},
        ),
        {"red_cycle": behaviour["red_cycle"]},
    )


async def owe_contract_again(
    connection: Connection, project_id: str, behaviour_id: str, reason: str
) -> None:
    """A red that showed the contract cannot serve: the Verifier is owed another."""
    behaviour = await (
        await connection.execute(
            "SELECT correlation_id, story_id, red_cycle FROM behaviours "
            "WHERE project_id = %s AND behaviour_id = %s",
            (project_id, behaviour_id),
        )
    ).fetchone()
    assert behaviour is not None
    if await _is_owed(connection, project_id, SPECIFY, behaviour_id=behaviour_id):
        return
    await _owe(
        connection,
        SPECIFY,
        Envelope(
            message_kind=MessageKind.COMMAND,
            event_type="behaviour.contract_requested",
            project_id=project_id,
            behaviour_id=behaviour_id,
            sender=CONTROL_PLANE,
            recipient="verifier",
            idempotency_key=f"respecify:{project_id}:{behaviour_id}:{uuid.uuid7()}",
            correlation_id=behaviour["correlation_id"],
            payload={"invalid_red": reason},
        ),
        {"red_cycle": behaviour["red_cycle"]},
    )


async def replacement_undecided(
    connection: Connection, project_id: str, behaviour_id: str
) -> dict[str, Any] | None:
    """The replacement this behaviour makes of something on trunk, if whether it stands
    is not settled: proposed and before the Product Owner, or approved once and ended by
    a decision since, with the Verifier yet to say whether it is proposed again. Until
    it is settled the behaviour's red waits and nothing is built: a candidate would only
    fail the very check it is meant to replace."""
    latest = await (
        await connection.execute(
            "SELECT s.supersession_id, s.state, o.behaviour_id AS replaced FROM supersessions s "
            "JOIN obligations o USING (obligation_id) WHERE s.project_id = %s "
            "AND s.behaviour_id = %s ORDER BY s.proposed_at DESC LIMIT 1",
            (project_id, behaviour_id),
        )
    ).fetchone()
    if latest is None or latest["state"] not in ("proposed", "void"):
        return None
    if latest["state"] == "void":
        answered = await (
            await connection.execute(
                "SELECT 1 FROM assignments WHERE project_id = %s AND behaviour_id = %s "
                "AND kind = 'specify' AND state = 'done' AND issued_on->>'voided' = %s",
                (project_id, behaviour_id, str(latest["supersession_id"])),
            )
        ).fetchone()
        if answered is not None:
            return None
    return latest


async def _owe_the_replacement_again(
    connection: Connection, project_id: str, behaviour: dict[str, Any]
) -> None:
    """An approval that a decision ended leaves two contracts that still contradict
    each other, and only the Verifier can put them before the Product Owner again."""
    undecided = await replacement_undecided(connection, project_id, behaviour["behaviour_id"])
    if undecided is None or undecided["state"] != "void":
        return
    if await _is_owed(connection, project_id, SPECIFY, behaviour_id=behaviour["behaviour_id"]):
        return
    await _owe(
        connection,
        SPECIFY,
        Envelope(
            message_kind=MessageKind.COMMAND,
            event_type="behaviour.contract_requested",
            project_id=project_id,
            behaviour_id=behaviour["behaviour_id"],
            sender=CONTROL_PLANE,
            recipient="verifier",
            idempotency_key=f"replace-again:{undecided['supersession_id']}",
            correlation_id=behaviour["correlation_id"],
            payload={"voided": str(undecided["supersession_id"])},
        ),
        {"voided": str(undecided["supersession_id"]), "replaced": undecided["replaced"]},
    )


async def _owe_for(connection: Connection, story: dict[str, Any]) -> None:
    project_id, story_id = story["project_id"], story["story_id"]
    behaviours = await (
        await connection.execute(
            "SELECT b.behaviour_id, b.state, b.correlation_id, "
            "(SELECT e.revision FROM executions e WHERE e.execution_id = b.red_execution_id) "
            "AS red_revision, b.red_execution_id FROM behaviours b "
            "WHERE b.project_id = %s AND b.story_id = %s ORDER BY b.position",
            (project_id, story_id),
        )
    ).fetchall()
    for behaviour in behaviours:
        if behaviour["state"] not in _ON_TRUNK:
            await _owe_the_replacement_again(connection, project_id, behaviour)
        if behaviour["state"] == "validating_red":
            await owe_red(connection, project_id, behaviour["behaviour_id"])
        elif behaviour["state"] == "implementing" and behaviour["red_revision"] is not None:
            await _owe_build(connection, project_id, behaviour)
    if any(behaviour["state"] not in _ON_TRUNK for behaviour in behaviours):
        return
    stands = {
        "story_revision": story["revision"],
        "list_revision": story["list_revision"],
        "basis": story["basis"],
    }
    if story["seal_id"] is None or story["sealed_revision"] != story["list_revision"]:
        kind = SEAL if story["demonstrated"] == stands else SPECIFY
        event = "story.seal_requested" if kind == SEAL else "behaviour.specification_requested"
        await _owe_step(connection, story, kind, "verifier", event, stands)
        return
    assessed = await (
        await connection.execute(
            "SELECT 1 FROM story_assessments WHERE seal_id = %s", (story["seal_id"],)
        )
    ).fetchone()
    if assessed is None:
        await _owe_step(
            connection,
            story,
            ASSESS,
            "analyst",
            "story.assessment_requested",
            {**stands, "seal_id": str(story["seal_id"])},
        )


async def _owe_step(
    connection: Connection,
    story: dict[str, Any],
    kind: str,
    role: str,
    event_type: str,
    issued_on: dict[str, Any],
) -> None:
    project_id, story_id = story["project_id"], story["story_id"]
    if await _is_owed(connection, project_id, kind, story_id=story_id):
        return
    await _owe(
        connection,
        kind,
        Envelope(
            message_kind=MessageKind.COMMAND,
            event_type=event_type,
            project_id=project_id,
            story_id=story_id,
            sender=CONTROL_PLANE,
            recipient=role,
            idempotency_key=f"{kind}:{project_id}:{story_id}:{uuid.uuid7()}",
            correlation_id=uuid.uuid7(),
            payload={"story_id": story_id, "feature_id": story["feature_id"]},
        ),
        issued_on,
        feature_id=story["feature_id"],
    )


async def _owe_build(connection: Connection, project_id: str, behaviour: dict[str, Any]) -> None:
    owed = await (
        await connection.execute(
            "SELECT 1 FROM assignments WHERE project_id = %s AND behaviour_id = %s "
            "AND kind IN ('build', 'remediate') AND state IN ('wanted', 'granted')",
            (project_id, behaviour["behaviour_id"]),
        )
    ).fetchone()
    if owed is not None:
        return
    await _owe(
        connection,
        BUILD,
        Envelope(
            message_kind=MessageKind.COMMAND,
            event_type="build.requested",
            project_id=project_id,
            behaviour_id=behaviour["behaviour_id"],
            sender=CONTROL_PLANE,
            recipient="builder",
            idempotency_key=f"build:{project_id}:{behaviour['behaviour_id']}:{uuid.uuid7()}",
            correlation_id=behaviour["correlation_id"],
            payload={"base_revision": behaviour["red_revision"]},
        ),
        {"red_execution_id": str(behaviour["red_execution_id"])},
    )


async def _is_owed(
    connection: Connection,
    project_id: str,
    kind: str,
    *,
    story_id: str | None = None,
    behaviour_id: str | None = None,
) -> bool:
    row = await (
        await connection.execute(
            "SELECT 1 FROM assignments WHERE project_id = %s AND kind = %s "
            "AND state IN ('wanted', 'granted') AND behaviour_id IS NOT DISTINCT FROM %s "
            "AND (%s::text IS NULL OR story_id = %s)",
            (project_id, kind, behaviour_id, story_id, story_id),
        )
    ).fetchone()
    return row is not None


async def _owe(
    connection: Connection,
    kind: str,
    command: Envelope,
    issued_on: dict[str, Any],
    feature_id: str | None = None,
) -> None:
    """Owe it, unless another transaction owed the same a moment ago: the database
    keeps one of each at a time, and the one that is there is the one that is owed."""
    try:
        async with connection.transaction():
            await owe(connection, kind, command, issued_on=issued_on, feature_id=feature_id)
    except errors.UniqueViolation:
        return
