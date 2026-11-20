"""Assignments: the work Relay wants done, and who has been granted it.

An assignment is created in the transaction that makes the work owed. A behaviour is
built for one reason at a time: a remediation can only become owed after the build
before it was completed by an accepted green, and the database refuses a second live one.
A command only announces it. A worker begins by claiming it,
which grants it, and the first durable effect of the work names that grant: a grant that
was taken back by a pause, or whose assignment was replaced or withdrawn, starts nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Any

from psycopg.types.json import Jsonb

from relay.application.closure import closure_of_behaviour, lock_gate, require_open
from relay.domain.story import ActionRefused, Principal, PrincipalKind
from relay.persistence.database import Connection, Pool
from relay.persistence.ledger import append_event
from relay.persistence.outbox import enqueue
from relay.transport.envelope import Envelope

BUILD = "build"
REMEDIATE = "remediate"
REVIEW = "review"
INTEGRATE = "integrate"
_LIVE = ("wanted", "granted")


async def owe(
    connection: Connection,
    kind: str,
    command: Envelope,
    *,
    issued_on: dict[str, Any],
) -> uuid.UUID:
    """Record that this work is owed and announce it, in the caller's transaction. The
    command is addressed to the role that may claim it and carries the assignment's id."""
    assert command.behaviour_id is not None
    assignment_id = uuid.uuid7()
    command = replace(command, payload={**command.payload, "assignment_id": str(assignment_id)})
    await connection.execute(
        "INSERT INTO assignments (assignment_id, project_id, behaviour_id, kind, role, "
        "issued_on, command_message_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            assignment_id,
            command.project_id,
            command.behaviour_id,
            kind,
            command.recipient,
            Jsonb(issued_on),
            command.message_id,
        ),
    )
    await enqueue(connection, f"relay:{command.project_id}:cmd:{command.recipient}", command)
    return assignment_id


async def retire_all_of_behaviour(
    connection: Connection, project_id: str, behaviour_id: str
) -> None:
    await _retire(connection, "project_id = %s AND behaviour_id = %s", (project_id, behaviour_id))


async def retire_all_of_feature(connection: Connection, project_id: str, feature_id: str) -> None:
    await _retire(connection, f"project_id = %s AND {_IN_FEATURE}", (project_id, feature_id))


async def revoke_unstarted_grants(connection: Connection, project_id: str, feature_id: str) -> None:
    """A pause takes back every grant of the feature that has had no effect yet. Called
    under the exclusive gate lock, so no first effect is in flight."""
    await _revoke(
        connection, project_id, f"first_effect_at IS NULL AND {_IN_FEATURE}", (feature_id,)
    )


async def announce_again(connection: Connection, project_id: str) -> int:
    """Have the command of every wanted assignment of an open feature sent again, as the
    same message. A consumer that has already been granted it does nothing new. The
    update waits for a publisher that is sending the same command at this moment, so the
    request stands even if that send is then lost."""
    again = await (
        await connection.execute(
            "UPDATE outbox SET published_at = NULL WHERE message_id IN "
            "(SELECT a.command_message_id FROM assignments a "
            "JOIN behaviours b USING (project_id, behaviour_id) "
            "JOIN stories s USING (project_id, story_id) "
            "JOIN features f USING (project_id, feature_id) "
            "WHERE a.project_id = %s AND a.state = 'wanted' AND f.closed_by IS NULL) "
            "RETURNING message_id",
            (project_id,),
        )
    ).fetchall()
    return len(again)


@dataclass(frozen=True)
class GreenRun:
    run_id: uuid.UUID
    assignment_id: uuid.UUID
    grant_id: uuid.UUID


async def start_green_run(
    connection: Connection,
    grant_id: uuid.UUID,
    *,
    project_id: str,
    behaviour_id: str,
    attempt: int,
) -> GreenRun:
    """The first effect of a build or a remediation: a record, committed before anything
    is launched, that this grant began a green run. Refused unless the grant is still the
    current one for this behaviour's build."""
    assignment_id = await _first_effect(
        connection,
        grant_id,
        "project_id = %s AND behaviour_id = %s AND kind = ANY(%s)",
        (project_id, behaviour_id, [BUILD, REMEDIATE]),
    )
    if assignment_id is None:
        raise ActionRefused("this grant is not the current one for this behaviour's build")
    run = GreenRun(uuid.uuid7(), assignment_id, grant_id)
    await connection.execute(
        "INSERT INTO green_runs (run_id, assignment_id, grant_id, attempt) VALUES (%s, %s, %s, %s)",
        (run.run_id, run.assignment_id, grant_id, attempt),
    )
    return run


async def start_review(
    connection: Connection, grant_id: uuid.UUID, assignment_id: uuid.UUID | None
) -> None:
    """Called in the transaction that records a review's checkout receipt, its first
    effect. Refused unless the grant is still the current one for that review."""
    if await _first_effect(connection, grant_id, "assignment_id = %s", (assignment_id,)) is None:
        raise ActionRefused("this grant is not the current one for this review")


async def holds(
    connection: Connection, grant_id: uuid.UUID, assignment_id: uuid.UUID | None
) -> bool:
    """Whether the grant is the assignment's current one, read without a lock: enough to
    turn a stale caller away early, never enough to start anything."""
    row = await (
        await connection.execute(
            "SELECT 1 FROM assignments WHERE grant_id = %s AND assignment_id = %s "
            "AND state = 'granted'",
            (grant_id, assignment_id),
        )
    ).fetchone()
    return row is not None


async def take_up(connection: Connection, assignment_id: uuid.UUID, consumer: str) -> None:
    """The control plane grants itself work of its own and records its effect, all in the
    transaction that makes the effect: there is no moment at which the grant exists and
    the effect does not."""
    await connection.execute(
        "UPDATE assignments SET state = 'done', grant_id = %s, granted_to = %s, "
        "first_effect_at = now() WHERE assignment_id = %s",
        (uuid.uuid7(), consumer, assignment_id),
    )


async def retire(connection: Connection, assignment_id: uuid.UUID) -> None:
    await _retire(connection, "assignment_id = %s", (assignment_id,))


async def revoke_every_grant(connection: Connection, project_id: str) -> None:
    """On start: whoever held a grant was in a process that is gone. Every grant is taken
    back, with or without a first effect, and a green run left unfinished is marked so.
    What those workers recorded stays as history."""
    await connection.execute(
        "UPDATE green_runs SET state = 'interrupted' WHERE state = 'started' AND assignment_id "
        "IN (SELECT assignment_id FROM assignments WHERE project_id = %s)",
        (project_id,),
    )
    await _revoke(connection, project_id, "TRUE", ())


async def retire_reviews_of_cycle(connection: Connection, cycle_id: uuid.UUID) -> None:
    await _retire(
        connection,
        "assignment_id IN (SELECT assignment_id FROM review_tasks WHERE cycle_id = %s)",
        (cycle_id,),
    )


async def finish_green_run(connection: Connection, run: GreenRun) -> bool:
    """Close the run's record and say whether the work it answers is still owed to this
    grant. Called under the behaviour's lock, in the transaction that judges the run."""
    await connection.execute(
        "UPDATE green_runs SET state = 'finished' WHERE run_id = %s", (run.run_id,)
    )
    current = await (
        await connection.execute(
            "SELECT 1 FROM assignments WHERE assignment_id = %s AND state = 'granted' "
            "AND grant_id = %s FOR UPDATE",
            (run.assignment_id, run.grant_id),
        )
    ).fetchone()
    return current is not None


async def complete(connection: Connection, assignment_id: uuid.UUID) -> None:
    """The effect the assignment asked for is on record."""
    await connection.execute(
        "UPDATE assignments SET state = 'done' WHERE assignment_id = %s", (assignment_id,)
    )


class Assignments:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def claim(self, principal: Principal, assignment_id: uuid.UUID) -> uuid.UUID:
        """Grant a wanted assignment of an open feature to this worker, and return the
        grant. The same worker asking again is given the same grant."""
        async with self._pool.connection() as connection:
            known = await (
                await connection.execute(
                    "SELECT project_id FROM assignments WHERE assignment_id = %s",
                    (assignment_id,),
                )
            ).fetchone()
        if known is None:
            raise ActionRefused("there is no such assignment")
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, known["project_id"])
            assignment = await (
                await connection.execute(
                    "SELECT * FROM assignments WHERE assignment_id = %s FOR UPDATE",
                    (assignment_id,),
                )
            ).fetchone()
            assert assignment is not None
            if principal.kind is not PrincipalKind.WORKER or principal.role != assignment["role"]:
                raise ActionRefused(f"this work is for a {assignment['role']} to claim")
            project_id, behaviour_id = assignment["project_id"], assignment["behaviour_id"]
            require_open(await closure_of_behaviour(connection, project_id, behaviour_id))
            if assignment["state"] == "granted":
                if assignment["granted_to"] != principal.identity:
                    raise ActionRefused("this work is already granted to another worker")
                granted: uuid.UUID = assignment["grant_id"]
                return granted
            if assignment["state"] != "wanted":
                raise ActionRefused("this work is no longer wanted")
            grant_id = uuid.uuid7()
            await connection.execute(
                "UPDATE assignments SET state = 'granted', grant_id = %s, granted_to = %s "
                "WHERE assignment_id = %s",
                (grant_id, principal.identity, assignment_id),
            )
            await append_event(
                connection,
                project_id,
                "assignment.granted",
                subject_id=behaviour_id,
                payload={
                    "assignment_id": str(assignment_id),
                    "grant_id": str(grant_id),
                    "kind": assignment["kind"],
                    "worker": principal.identity,
                },
            )
            return grant_id


_IN_FEATURE = (
    "behaviour_id IN (SELECT b.behaviour_id FROM behaviours b "
    "JOIN stories s USING (project_id, story_id) "
    "WHERE b.project_id = assignments.project_id AND s.feature_id = %s)"
)


async def _revoke(
    connection: Connection, project_id: str, where: str, parameters: tuple[Any, ...]
) -> None:
    revoked = await (
        await connection.execute(
            "SELECT assignment_id, behaviour_id, grant_id, granted_to FROM assignments "
            f"WHERE project_id = %s AND state = 'granted' AND {where} "
            "ORDER BY assignment_id FOR UPDATE",
            (project_id, *parameters),
        )
    ).fetchall()
    await connection.execute(
        "UPDATE assignments SET state = 'wanted', grant_id = NULL, granted_to = NULL, "
        "first_effect_at = NULL WHERE assignment_id = ANY(%s)",
        ([grant["assignment_id"] for grant in revoked],),
    )
    for grant in revoked:
        await append_event(
            connection,
            project_id,
            "assignment.grant_revoked",
            subject_id=grant["behaviour_id"],
            payload={
                "assignment_id": str(grant["assignment_id"]),
                "grant_id": str(grant["grant_id"]),
                "worker": grant["granted_to"],
            },
        )


async def _first_effect(
    connection: Connection, grant_id: uuid.UUID, where: str, parameters: tuple[Any, ...]
) -> uuid.UUID | None:
    row = await (
        await connection.execute(
            "UPDATE assignments SET first_effect_at = COALESCE(first_effect_at, now()) "
            f"WHERE grant_id = %s AND state = 'granted' AND {where} RETURNING assignment_id",
            (grant_id, *parameters),
        )
    ).fetchone()
    return row["assignment_id"] if row else None


async def _retire(connection: Connection, where: str, parameters: tuple[Any, ...]) -> None:
    await connection.execute(
        "UPDATE assignments SET state = 'retired', grant_id = NULL, granted_to = NULL "
        f"WHERE state = ANY(%s) AND {where}",
        (list(_LIVE), *parameters),
    )
