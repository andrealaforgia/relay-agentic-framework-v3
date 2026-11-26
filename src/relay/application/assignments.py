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
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from psycopg.types.json import Jsonb

from relay.application.closure import (
    closure_of_feature,
    lock_gate,
    require_open,
    require_startable,
)
from relay.application.workers import is_alive
from relay.domain.story import ActionRefused, Principal, PrincipalKind
from relay.persistence.database import Connection, Pool
from relay.persistence.ledger import append_event
from relay.persistence.outbox import enqueue
from relay.transport.envelope import Envelope, MessageKind

BUILD = "build"
REMEDIATE = "remediate"
REVIEW = "review"
INTEGRATE = "integrate"
COMPLETE = "complete"
REVISE = "revise"
RECONFIRM = "reconfirm"
CORRECT = "correct"
PLAN = "plan"
SPECIFY = "specify"
SEAL = "seal"
ASSESS = "assess"
RED = "red"
# Work that changes nothing but the roadmap's proposals.
ROADMAP_WORK = (PLAN, REVISE, CORRECT)
_LIVE = ("wanted", "granted")


async def owe(
    connection: Connection,
    kind: str,
    command: Envelope,
    *,
    issued_on: dict[str, Any],
    feature_id: str | None = None,
    succeeds: uuid.UUID | None = None,
) -> uuid.UUID:
    """Record that this work is owed and announce it, in the caller's transaction. The
    command is addressed to the role that may claim it and carries the assignment's id.
    Its subject is the behaviour the command names; failing that the story; failing
    that the roadmap of the feature given."""
    assignment_id = uuid.uuid7()
    command = replace(command, payload={**command.payload, "assignment_id": str(assignment_id)})
    story_id = command.story_id
    if command.behaviour_id is not None:
        subject = "behaviour"
        located = await (
            await connection.execute(
                "SELECT s.feature_id, s.story_id FROM behaviours b "
                "JOIN stories s USING (project_id, story_id) "
                "WHERE b.project_id = %s AND b.behaviour_id = %s",
                (command.project_id, command.behaviour_id),
            )
        ).fetchone()
        assert located is not None
        feature_id, story_id = located["feature_id"], located["story_id"]
    else:
        subject = "story" if story_id is not None else "roadmap"
    assert feature_id is not None
    await connection.execute(
        "INSERT INTO assignments (assignment_id, project_id, subject, feature_id, story_id, "
        "behaviour_id, kind, role, issued_on, command_message_id, succeeds) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            assignment_id,
            command.project_id,
            subject,
            feature_id,
            story_id,
            command.behaviour_id,
            kind,
            command.recipient,
            Jsonb(issued_on),
            command.message_id,
            succeeds,
        ),
    )
    await enqueue(connection, f"relay:{command.project_id}:cmd:{command.recipient}", command)
    return assignment_id


async def owe_again(
    connection: Connection, predecessor: uuid.UUID, *, state: str, reason: str
) -> uuid.UUID:
    """One occasion on which work was owed is over, in `state`, and the same work is owed
    again: a new assignment that names the one it succeeds, announced by a command of
    its own that says why. A redelivery of the old command finds its assignment
    finished and calls nothing; the new one admits a call of its own."""
    earlier = await (
        await connection.execute(
            "SELECT a.*, o.envelope FROM assignments a JOIN outbox o "
            "ON o.message_id = a.command_message_id WHERE a.assignment_id = %s FOR UPDATE OF a",
            (predecessor,),
        )
    ).fetchone()
    assert earlier is not None
    await connection.execute(
        "UPDATE assignments SET state = %s, parked = NULL WHERE assignment_id = %s",
        (state, predecessor),
    )
    was = earlier["envelope"]
    payload = {
        name: value
        for name, value in was["payload"].items()
        if name not in ("assignment_id", "succeeds", "reason")
    }
    return await owe(
        connection,
        earlier["kind"],
        Envelope(
            message_kind=MessageKind.COMMAND,
            event_type=was["event_type"],
            project_id=earlier["project_id"],
            sender=was["sender"],
            recipient=earlier["role"],
            idempotency_key=f"again:{predecessor}",
            correlation_id=uuid.UUID(was["correlation_id"]),
            story_id=was["story_id"],
            behaviour_id=was["behaviour_id"],
            payload={**payload, "succeeds": str(predecessor), "reason": reason},
        ),
        issued_on=earlier["issued_on"],
        feature_id=earlier["feature_id"],
        succeeds=predecessor,
    )


async def ask_instead(connection: Connection, assignment_id: uuid.UUID) -> None:
    """The reply to this work was a question for the Product Owner: this occasion is
    over. Answering the question owes the work again."""
    await connection.execute(
        "UPDATE assignments SET state = 'asked' WHERE assignment_id = %s", (assignment_id,)
    )


async def retire_all_of_behaviour(
    connection: Connection, project_id: str, behaviour_id: str
) -> None:
    await _retire(connection, "project_id = %s AND behaviour_id = %s", (project_id, behaviour_id))


async def retire_all_of_feature(connection: Connection, project_id: str, feature_id: str) -> None:
    await _retire(connection, "project_id = %s AND feature_id = %s", (project_id, feature_id))


async def revoke_unstarted_grants(
    connection: Connection, project_id: str, feature_id: str, story_id: str | None = None
) -> None:
    """A pause, or a question, takes back every grant in its scope (the feature, or one
    story of it) that has had no effect yet. Called under the exclusive gate lock, so no
    first effect is in flight."""
    await _revoke(
        connection,
        project_id,
        "first_effect_at IS NULL AND feature_id = %s AND (%s::text IS NULL OR story_id = %s)",
        (feature_id, story_id, story_id),
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
            "WHERE a.project_id = %s AND a.state = 'wanted' AND a.parked IS NULL "
            "AND standing(a.project_id, a.feature_id, a.story_id) IS NULL) "
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
    # The decision basis the run began on.
    basis: int


async def start_green_run(
    connection: Connection,
    grant_id: uuid.UUID,
    *,
    project_id: str,
    behaviour_id: str,
    attempt: int,
    basis: int,
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
    run = GreenRun(uuid.uuid7(), assignment_id, grant_id, basis)
    await connection.execute(
        "INSERT INTO green_runs (run_id, assignment_id, grant_id, attempt, basis) "
        "VALUES (%s, %s, %s, %s, %s)",
        (run.run_id, run.assignment_id, grant_id, attempt, basis),
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
    """On start: whoever held a grant in the control plane's own process is gone with it.
    Each of those grants is taken back, with or without a first effect, and a green run
    left unfinished is marked so. What they recorded stays as history. A grant held by a
    worker process is not touched here: whether that worker is alive is asked of the
    database, and what it left is dealt with by `Invocations.recover`."""
    await connection.execute(
        "UPDATE green_runs SET state = 'interrupted' WHERE state = 'started' AND assignment_id "
        "IN (SELECT assignment_id FROM assignments WHERE project_id = %s "
        "AND granted_worker IS NULL)",
        (project_id,),
    )
    await _revoke(connection, project_id, "granted_worker IS NULL", ())


async def retire_all_of_story(connection: Connection, project_id: str, story_id: str) -> None:
    await _retire(connection, "project_id = %s AND story_id = %s", (project_id, story_id))


async def retire_what_a_roadmap_answers(
    connection: Connection,
    project_id: str,
    feature_id: str,
    *,
    revisions: Sequence[int],
    stories: Sequence[str] = (),
) -> None:
    """A roadmap revision that is approved, replaced or sent back settles what was owed
    for it; an approved one also settles what was owed for the revision it supersedes
    and for the stories it rewords or drops."""
    await _retire(
        connection,
        "project_id = %s AND feature_id = %s AND kind = 'reconfirm' AND ("
        "(subject = 'roadmap' AND (issued_on->>'revision')::integer = ANY(%s)) "
        "OR (subject = 'story' AND story_id = ANY(%s)))",
        (project_id, feature_id, list(revisions), list(stories)),
    )


async def retire_reconfirmations(
    connection: Connection, project_id: str, feature_id: str, story_id: str | None
) -> None:
    """A later decision in the same scope makes the reconfirmations owed for an earlier
    one pointless: new ones are owed for the new basis."""
    await _retire(
        connection,
        "project_id = %s AND feature_id = %s AND kind = 'reconfirm' "
        "AND (%s::text IS NULL OR story_id = %s)",
        (project_id, feature_id, story_id, story_id),
    )


async def complete_reconfirmation(
    connection: Connection,
    grant_id: uuid.UUID,
    project_id: str,
    subject: str,
    subject_id: str,
    revision: int | None = None,
) -> None:
    """A reconfirmation, or the amendment that stands in its place, is done under the
    grant that holds exactly this work now: this subject, of this project (a behaviour's
    or a story's name is only its project's own). Which basis it is for needs no checking
    here: a later decision retires the assignment, and a pause or a question takes the
    grant back, so a grant that still holds is for the basis in force."""
    done = await (
        await connection.execute(
            "UPDATE assignments SET state = 'done', first_effect_at = now() "
            "WHERE grant_id = %s AND state = 'granted' AND kind = 'reconfirm' "
            "AND project_id = %s AND subject = %s "
            "AND coalesce(behaviour_id, story_id, feature_id) = %s "
            # A feature can owe two for its roadmap: the one in force, and a proposal.
            "AND (%s::integer IS NULL OR (issued_on->>'revision')::integer = %s) "
            "RETURNING assignment_id",
            (grant_id, project_id, subject, subject_id, revision, revision),
        )
    ).fetchone()
    if done is None:
        raise ActionRefused("this grant is not the current one for this reconfirmation")


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

    async def give_back(self, assignment_id: uuid.UUID, grant_id: uuid.UUID) -> None:
        """A worker that will not, for now, do anything under its grant hands it back: the
        work is wanted again and is announced again.

        What decides is what the grant owns at this moment, not what it ever did. A
        grant whose work has a call in flight, or a known result waiting to be applied
        (its own, or one it adopted), keeps it: that is dealt with by what the call
        left. A grant whose work has had no call, or whose last call gave nothing, owns
        nothing, and holding on to it would leave the work with a worker that has said
        it will not do it."""
        async with self._pool.connection() as connection, connection.transaction():
            known = await (
                await connection.execute(
                    "SELECT project_id FROM assignments WHERE assignment_id = %s",
                    (assignment_id,),
                )
            ).fetchone()
            if known is None:
                return
            await lock_gate(connection, known["project_id"])
            await _revoke(
                connection,
                known["project_id"],
                "assignment_id = %s AND grant_id = %s AND coalesce((SELECT v.state FROM "
                "invocations v WHERE v.assignment_id = assignments.assignment_id "
                "ORDER BY v.attempt DESC LIMIT 1), 'none') NOT IN ('intended', 'returned')",
                (assignment_id, grant_id),
            )
            await connection.execute(
                "UPDATE outbox SET published_at = NULL WHERE message_id = "
                "(SELECT command_message_id FROM assignments WHERE assignment_id = %s "
                "AND state = 'wanted' AND parked IS NULL)",
                (assignment_id,),
            )

    async def of(self, assignment_id: uuid.UUID) -> dict[str, Any] | None:
        """The work a command announces, as it stands now; None if there is none."""
        async with self._pool.connection() as connection:
            return await (
                await connection.execute(
                    "SELECT * FROM assignments WHERE assignment_id = %s", (assignment_id,)
                )
            ).fetchone()

    async def claim(
        self, principal: Principal, assignment_id: uuid.UUID, worker: uuid.UUID | None = None
    ) -> uuid.UUID:
        """Grant a wanted assignment of an open feature to this worker, and return the
        grant. The same worker asking again is given the same grant. `worker` is the
        worker process the grant belongs to, which has to be alive: when it is gone, the
        pass deals with what it left. A grant with no worker is one made by a caller in
        the control plane's own process, and ends with that process."""
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
            if assignment["kind"] in (INTEGRATE, COMPLETE, RED):
                # Its grant and its first effect are made together, by the control plane.
                # A grant on its own would leave the checks never started.
                raise ActionRefused(
                    "the control plane takes up integrations, completions and reds itself"
                )
            if principal.kind is not PrincipalKind.WORKER or principal.role != assignment["role"]:
                raise ActionRefused(f"this work is for a {assignment['role']} to claim")
            project_id = assignment["project_id"]
            subject_id = (
                assignment["behaviour_id"] or assignment["story_id"] or assignment["feature_id"]
            )
            ordinary = assignment["kind"] not in (RECONFIRM, CORRECT)
            if ordinary and assignment["story_id"] is not None:
                # Ordinary work relies on the story, the contract and the evidence so
                # far being current. Reconfirming is how they become so, and a
                # correction is owed for a story that is finished with.
                await require_startable(
                    connection, project_id, assignment["story_id"], assignment["behaviour_id"]
                )
            require_open(
                await closure_of_feature(
                    connection, project_id, assignment["feature_id"], assignment["story_id"]
                )
            )
            await require_nothing_to_reconfirm(connection, assignment)
            if (
                assignment["kind"] == RECONFIRM
                and assignment["behaviour_id"] is not None
                and await _has_an_unsettled_integration(
                    connection, project_id, assignment["behaviour_id"]
                )
            ):
                # The Verifier is asked to vouch for what is on record, and what will be
                # on record is not known until the push has its answer.
                raise ActionRefused(
                    f"the integration of {assignment['behaviour_id']} has not settled yet"
                )
            if assignment["kind"] in ROADMAP_WORK and await _has_a_proposal_waiting(
                connection, project_id, assignment["feature_id"]
            ):
                # One proposal at a time is before the Product Owner. What is decided
                # there says what is still owed, and announces it again.
                raise ActionRefused(
                    "a proposal for this roadmap is waiting to be decided: nothing more "
                    "is proposed until it is"
                )
            if assignment["parked"] is not None:
                raise ActionRefused(f"this work is parked: {assignment['parked']}")
            if worker is not None:
                registered = await (
                    await connection.execute(
                        "SELECT role FROM workers WHERE worker_id = %s", (worker,)
                    )
                ).fetchone()
                # The caller, the worker process and the role are one thing or nothing
                # is granted: what the pass later concludes from that process being gone
                # must be about the caller that holds the grant.
                if (
                    registered is None
                    or registered["role"] != principal.role
                    or principal.identity != f"{principal.role}@{worker}"
                ):
                    raise ActionRefused("this worker is not the one asking")
                if not await is_alive(connection, worker):
                    raise ActionRefused("this worker is not running")
            if assignment["state"] == "granted":
                if assignment["granted_to"] != principal.identity:
                    raise ActionRefused("this work is already granted to another worker")
                granted: uuid.UUID = assignment["grant_id"]
                return granted
            if assignment["state"] != "wanted":
                raise ActionRefused("this work is no longer wanted")
            grant_id = uuid.uuid7()
            await connection.execute(
                "UPDATE assignments SET state = 'granted', grant_id = %s, granted_to = %s, "
                "granted_worker = %s WHERE assignment_id = %s",
                (grant_id, principal.identity, worker, assignment_id),
            )
            await append_event(
                connection,
                project_id,
                "assignment.granted",
                subject_id=subject_id,
                payload={
                    "assignment_id": str(assignment_id),
                    "grant_id": str(grant_id),
                    "kind": assignment["kind"],
                    "worker": principal.identity,
                },
            )
            return grant_id


async def _revoke(
    connection: Connection, project_id: str, where: str, parameters: tuple[Any, ...]
) -> None:
    revoked = await (
        await connection.execute(
            "SELECT assignment_id, coalesce(behaviour_id, story_id, feature_id) AS behaviour_id, "
            "grant_id, granted_to FROM assignments "
            f"WHERE project_id = %s AND state = 'granted' AND {where} "
            "ORDER BY assignment_id FOR UPDATE",
            (project_id, *parameters),
        )
    ).fetchall()
    await connection.execute(
        "UPDATE assignments SET state = 'wanted', grant_id = NULL, granted_to = NULL, "
        "granted_worker = NULL, first_effect_at = NULL WHERE assignment_id = ANY(%s)",
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


async def require_nothing_to_reconfirm(connection: Connection, assignment: dict[str, Any]) -> None:
    """Work on a story as a whole (what it needs next, its seal, its assessment) relies
    on every contract of it standing. Until each owner has answered for what a decision
    put in question, none of it starts: a call made now could not be applied."""
    if assignment["kind"] not in (SPECIFY, SEAL, ASSESS) or assignment["behaviour_id"] is not None:
        return
    owed = await (
        await connection.execute(
            "SELECT coalesce(behaviour_id, story_id) AS subject FROM assignments "
            "WHERE project_id = %s AND story_id = %s AND kind = 'reconfirm' "
            "AND state IN ('wanted', 'granted') ORDER BY created_at LIMIT 1",
            (assignment["project_id"], assignment["story_id"]),
        )
    ).fetchone()
    if owed is not None:
        raise ActionRefused(
            f"{owed['subject']} has yet to be reconfirmed by its owner: nothing new starts here"
        )


async def _has_a_proposal_waiting(connection: Connection, project_id: str, feature_id: str) -> bool:
    row = await (
        await connection.execute(
            # Waiting before the Product Owner, that is: a proposal whose gate a decision
            # closed is waiting for its author, and may be replaced.
            "SELECT 1 FROM roadmaps r WHERE r.project_id = %s AND r.feature_id = %s "
            "AND r.state = 'proposed' AND EXISTS (SELECT 1 FROM gates g "
            "WHERE g.project_id = r.project_id AND g.feature_id = r.feature_id "
            "AND g.kind = 'roadmap' AND g.state = 'open')",
            (project_id, feature_id),
        )
    ).fetchone()
    return row is not None


async def _has_an_unsettled_integration(
    connection: Connection, project_id: str, behaviour_id: str
) -> bool:
    row = await (
        await connection.execute(
            "SELECT 1 FROM integration_intents WHERE project_id = %s AND behaviour_id = %s "
            "AND state = 'outstanding'",
            (project_id, behaviour_id),
        )
    ).fetchone()
    return row is not None


async def _retire(connection: Connection, where: str, parameters: tuple[Any, ...]) -> None:
    retired = await (
        await connection.execute(
            "UPDATE assignments SET state = 'retired', grant_id = NULL, granted_to = NULL, "
            f"parked = NULL WHERE state = ANY(%s) AND {where} RETURNING assignment_id",
            (list(_LIVE), *parameters),
        )
    ).fetchall()
    # Work that is ended asks nothing more of anyone: a call of it that a person was
    # being asked about leaves the inbox with it.
    await connection.execute(
        "UPDATE inbox_items SET state = 'closed' WHERE state = 'open' AND invocation_id IN "
        "(SELECT invocation_id FROM invocations WHERE assignment_id = ANY(%s))",
        ([each["assignment_id"] for each in retired],),
    )
