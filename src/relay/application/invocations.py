"""Calls to a provider: when one may be made, what became of it, and how its result is
applied once.

A call costs money and cannot be taken back, so the record comes first. One function,
`admit`, decides whether a provider may be called for an assignment, and nothing calls
one without it. What the call returned is stored before anything is done with it. Its
effects are then carried out one at a time, each taken, performed and marked, so that a
process that stops anywhere is carried on from the record and the model is never asked
twice for what it has already said.

An assignment is one occasion on which work is owed: within it, a delivery that comes
again finds what the first one left.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from relay.application.assignments import BUILD, CORRECT, RECONFIRM, REMEDIATE, REVIEW
from relay.application.closure import (
    closure_of_feature,
    feature_basis,
    lock_gate,
    require_open,
    require_startable,
    story_basis,
)
from relay.application.stories import _record as record_receipt
from relay.application.stories import _recorded as recorded_receipt
from relay.application.workers import WorkerSession, is_alive
from relay.domain.schema import problem_with as schema_problem
from relay.domain.schema import require_checkable
from relay.domain.story import (
    ActionRefused,
    HumanAction,
    Receipt,
    answer_again,
    require_human,
    require_well_formed,
)
from relay.persistence.database import Connection, Pool
from relay.persistence.ledger import append_event
from relay.providers.claude import Outcome, ProviderResult
from relay.providers.definitions import AgentDefinition
from relay.providers.pricing import PRICES, PriceTable, estimate

# Calls that gave nothing usable, per assignment, before a person is asked.
ATTEMPTS_BEFORE_ASKING = 3
ON_A_PARKED_CALL = {"retry": frozenset[str](), "set_aside": frozenset[str]()}
_UNCERTAIN = "a paid call may have completed and its result was not received"


class Overtaken(Exception):
    """Raised by an effect that finds its result can no longer be applied through no
    fault of the reply: the review it was of is closed, or the evidence it was written
    from is no longer the grant's. The invocation is overtaken, and blamed for nothing."""


@dataclass(frozen=True)
class Prepared:
    """Everything about a call that is decided before it is admitted."""

    provider: str
    model: str
    effort: str | None
    definition: AgentDefinition
    # The hash of the definition as handed over: the file's, with the role's tools.
    effective_agent_sha256: str
    briefing: str
    schema: Mapping[str, Any]
    # The revisions and decision basis the briefing was written from, as
    # `Invocations.as_it_stands` gave them when preparation began.
    read_on: Mapping[str, Any]
    # What the model was handed besides the briefing, recorded with the call.
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Admission:
    """`admitted`: call the provider under this invocation. `apply`: a result is already
    known; apply it, call nothing. `finished`: nothing is left to do for this
    assignment. `refused`: nothing may be called now, for the reason given. `stale`:
    what the call was prepared from has changed; prepare it again."""

    outcome: str
    invocation_id: uuid.UUID | None = None
    attempt: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class EffectRun:
    """What an effect is given. `effect_id` is the same for every try of the effect, and
    is what the effect names whatever it does by, so that doing it again is doing it
    once. `attempt` says which try this is. `grant_id` is the grant to act under."""

    invocation: dict[str, Any]
    effect_id: str
    attempt: int
    grant_id: uuid.UUID


@dataclass(frozen=True)
class Effect:
    """One thing that applying a result does.

    `perform` runs outside any transaction and must be safe to run again under the same
    `effect_id`: it runs again if the process stops before the effect is marked. It may
    return a detail to keep. It must not change the revisions or the decision basis of
    what the invocation is about: nobody could then tell its change from someone else's.

    `settle`, if given, runs inside the transaction that marks the effect done, after
    that transaction has established that this try is the current one and that the work
    has neither ended, changed nor stopped. What an effect writes to PostgreSQL belongs
    there: it commits with the mark or not at all. If it changes the revisions or the
    decision basis of what the invocation is about, it returns what they now are, and
    the next effect expects that.
    """

    name: str
    perform: Callable[[EffectRun], Awaitable[Mapping[str, Any] | None]]
    settle: Callable[[Connection, EffectRun], Awaitable[Mapping[str, Any] | None]] | None = None


@dataclass(frozen=True)
class Parked:
    invocation_id: uuid.UUID
    assignment_id: uuid.UUID
    role: str
    state: str
    reason: str


EffectsFor = Callable[[dict[str, Any]], Sequence[Effect]]


class Invocations:
    def __init__(
        self, pool: Pool, effects_for: EffectsFor | None = None, prices: PriceTable = PRICES
    ) -> None:
        self._pool = pool
        # How a known result is applied, by whoever has to apply it: the worker that
        # obtained it, or the pass, if that worker is gone.
        self._effects_for = effects_for
        self._prices = prices
        self._session: WorkerSession | None = None
        self._starting = asyncio.Lock()
        self._giving_up: set[asyncio.Task[None]] = set()

    async def admit(
        self,
        worker: WorkerSession,
        assignment_id: uuid.UUID,
        grant_id: uuid.UUID,
        prepared: Prepared,
        *,
        authorised: Callable[[Connection], Awaitable[uuid.UUID | None]] | None = None,
    ) -> Admission:
        """Decide, under the assignment's lock, whether a provider may be called now.

        `authorised` says, asked here and not before, which assessment of the run's
        boundary allows the launch, or None if none does. Admission can wait a long time
        for its locks, and an answer got before that wait is about another moment."""
        async with self._pool.connection() as connection, connection.transaction():
            assignment = await _locked_assignment(connection, assignment_id)
            if assignment["state"] in ("done", "retired", "asked"):
                return Admission("finished")
            if assignment["state"] != "granted" or assignment["grant_id"] != grant_id:
                return Admission("refused", reason="this grant is not the current one")
            if assignment["granted_worker"] != worker.worker_id:
                # Also what refuses a grant with no worker at all: only a worker process,
                # whose being alive can be asked, may make a paid call.
                return Admission("refused", reason="this grant is not this worker's")
            if not await is_alive(connection, worker.worker_id):
                # It has lost the connection by which anyone knows it is alive. A call
                # it made now would be one nobody could account for.
                return Admission("refused", reason="this worker is no longer known to be running")
            latest = await _latest(connection, assignment_id)
            if latest is not None:
                state = latest["state"]
                if state == "returned":
                    return Admission("apply", latest["invocation_id"], latest["attempt"])
                if state == "intended":
                    return Admission("refused", reason="a call for this work is in flight")
                if state == "applied":
                    return Admission("finished")
                # Otherwise the last call gave nothing that was used: it was uncertain,
                # errored, rejected, never started, or overtaken. Work that is waiting on
                # a person is parked, and parked work has no grant to be here under. Work
                # whose last result was overtaken and which is still granted here is
                # still owed: what ended for good was retired, and never gets this far.
            try:
                # Every stop that would refuse a claim refuses a paid call, the first
                # and every one after it.
                await _require_may_start(connection, assignment)
            except ActionRefused as stopped:
                return Admission("refused", reason=str(stopped))
            stands = await _as_it_stands(connection, assignment)
            if dict(prepared.read_on) != stands:
                # The briefing was written from something that has changed since. The
                # call is not made on it, and is not recorded as made on what is here now.
                return Admission("stale", reason="what this was prepared from has changed")
            # Before anything is paid for: a reply could not be checked against a schema
            # that asks for what Relay does not check.
            require_checkable(dict(prepared.schema))
            assessment = await authorised(connection) if authorised is not None else None
            if authorised is not None and assessment is None:
                return Admission(
                    "refused",
                    reason="the bounds of this run have not been shown to hold on this host",
                )
            invocation_id = uuid.uuid7()
            attempt = (latest["attempt"] if latest is not None else 0) + 1
            admitted_on = {**stands, "grant_id": str(grant_id), "attempt": attempt}
            if assessment is not None:
                # Which run of the boundary suite allowed this launch.
                admitted_on["boundary_assessment"] = str(assessment)
            await connection.execute(
                "INSERT INTO invocations (invocation_id, assignment_id, project_id, grant_id, "
                "attempt, worker_id, role, provider, model, effort, agent_name, agent_path, "
                "agent_sha256, agent_revision, agent_dirty, effective_agent_sha256, briefing, "
                "briefing_sha256, schema_sha256, admitted_on, expects, schema, evidence) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s)",
                (
                    invocation_id,
                    assignment_id,
                    assignment["project_id"],
                    grant_id,
                    attempt,
                    worker.worker_id,
                    assignment["role"],
                    prepared.provider,
                    prepared.model,
                    prepared.effort,
                    prepared.definition.name,
                    str(prepared.definition.path),
                    prepared.definition.sha256,
                    prepared.definition.revision,
                    prepared.definition.dirty,
                    prepared.effective_agent_sha256,
                    prepared.briefing,
                    hashlib.sha256(prepared.briefing.encode()).hexdigest(),
                    _digest(prepared.schema),
                    Jsonb(admitted_on),
                    Jsonb(stands),
                    Jsonb(dict(prepared.schema)),
                    Jsonb(dict(prepared.evidence)),
                ),
            )
            # The paid call is the grant's first effect: a pause from here on leaves the
            # grant alone, and what the call returns is held, not lost.
            await connection.execute(
                "UPDATE assignments SET first_effect_at = COALESCE(first_effect_at, now()) "
                "WHERE assignment_id = %s",
                (assignment_id,),
            )
            await _note(connection, assignment, "invocation.intended", invocation_id, attempt)
            return Admission("admitted", invocation_id, attempt)

    async def record(
        self,
        invocation_id: uuid.UUID,
        result: ProviderResult,
        *,
        problem: str | None = None,
        retained: str | None = None,
    ) -> str:
        """Store what the provider returned, before anything is done with it, and say
        what state that leaves the call in. `problem` is why a reply that arrived cannot
        be used (it does not satisfy its schema, say); `retained` is the commit a build's
        workspace was kept as."""
        async with self._pool.connection() as connection, connection.transaction():
            invocation = await _located(connection, invocation_id)
            assignment = await _locked_assignment(connection, invocation["assignment_id"])
            invocation = await _locked_invocation(connection, invocation_id)
            late = invocation["state"] == "uncertain"
            if invocation["state"] not in ("intended", "uncertain"):
                return str(invocation["state"])
            # Work that has been ended meanwhile (retired, or done by other means) keeps
            # what is learned about its call, and nothing is asked or owed for it.
            live = assignment["state"] in ("wanted", "granted")
            # Whoever hands a result over, it is checked here against the schema that was
            # stored with the call: a late result delivered by other hands than the
            # worker's is read as nothing either until it has been.
            state, reason = _ending(
                result,
                problem or result.unusable or reply_problem(result, invocation["schema"]),
            )
            if late and result.terminal is None and result.outcome is not Outcome.NOT_STARTED:
                return "uncertain"  # nothing more is known than was
            if state == "returned" and assignment["kind"] in (BUILD, REMEDIATE) and not retained:
                # What a build returns is a tree. Without the tree kept there is nothing
                # to apply, whatever the model said about it.
                state, reason = "rejected", "nothing was kept of what the build made"
            if late and await _has_a_successor(connection, invocation):
                # Another call was admitted in its place. What this one returned is kept
                # as evidence, with what it used, and applied to nothing.
                state, reason = "overtaken", "its result arrived after another call was admitted"
            usage, observed, estimated = self._usage(result)
            adopted_under = None
            if late and live and state == "returned" and assignment["state"] == "granted":
                # The work was granted again before this result arrived, and no call has
                # been made under that grant. The result is known and paid for: it is
                # applied under the grant that holds the work now, and its own grant stays
                # on record as what it ran under.
                adopted_under = assignment["grant_id"]
                # Having a known result to apply is that grant's first effect: a pause
                # holds it, and does not take it back.
                await connection.execute(
                    "UPDATE assignments SET first_effect_at = COALESCE(first_effect_at, now()) "
                    "WHERE assignment_id = %s",
                    (assignment["assignment_id"],),
                )
            await connection.execute(
                "UPDATE invocations SET state = %s, reason = %s, envelope = %s, reply = %s, "
                "retained = %s, usage = %s, provider_cost_usd = %s, estimated_cost_usd = %s, "
                "price_table = %s, retry_authorised_by = NULL, applied_under = %s, "
                "ended_at = clock_timestamp() WHERE invocation_id = %s",
                (
                    state,
                    reason,
                    Jsonb(result.terminal.envelope) if result.terminal else None,
                    Jsonb(result.terminal.structured_output)
                    if state == "returned" and result.terminal
                    else None,
                    retained,
                    Jsonb(usage) if usage is not None else None,
                    observed,
                    estimated,
                    self._prices.version if estimated is not None else None,
                    adopted_under,
                    invocation_id,
                ),
            )
            if late and live and state != "overtaken":
                # The result is known after all: the work is no longer waiting on a
                # person, and if nobody has taken it up since, it is this call's again.
                await _unpark(connection, assignment)
            if late and live and state == "returned":
                await connection.execute(
                    "UPDATE assignments SET state = 'granted', grant_id = %s, granted_to = %s, "
                    "granted_worker = %s, first_effect_at = now() "
                    "WHERE assignment_id = %s AND state = 'wanted'",
                    (
                        invocation["grant_id"],
                        f"{invocation['role']}@{invocation['worker_id']}",
                        invocation["worker_id"],
                        assignment["assignment_id"],
                    ),
                )
            if not live:
                pass
            elif state == "not_started":
                await _release(connection, assignment)
            elif state == "uncertain":
                await _park(connection, assignment, invocation_id, _UNCERTAIN)
            elif state in ("rejected", "errored") and (
                await _calls_made(connection, assignment["assignment_id"]) >= ATTEMPTS_BEFORE_ASKING
            ):
                await _park(
                    connection,
                    assignment,
                    invocation_id,
                    f"{ATTEMPTS_BEFORE_ASKING} calls gave nothing that could be used",
                )
            await _note(
                connection, assignment, f"invocation.{state}", invocation_id, invocation["attempt"]
            )
            return state

    def _usage(
        self, result: ProviderResult
    ) -> tuple[list[dict[str, Any]] | None, Decimal | None, Decimal | None]:
        if result.terminal is None:
            return None, None, None
        used = result.terminal.usage
        estimates = [estimate(self._prices, each) for each in used]
        # An estimate of the whole only if every part of it could be estimated.
        estimated = (
            sum((each for each in estimates if each is not None), Decimal(0))
            if used and None not in estimates
            else None
        )
        reported = [
            {
                "model": each.model,
                "input_tokens": each.input_tokens,
                "output_tokens": each.output_tokens,
                "cache_read_tokens": each.cache_read_tokens,
                "cache_write_tokens": each.cache_write_tokens,
                "cache_write_5m_tokens": each.cache_write_5m_tokens,
                "cache_write_1h_tokens": each.cache_write_1h_tokens,
            }
            for each in used
        ]
        return reported, result.terminal.provider_cost_usd, estimated

    async def as_it_stands(self, assignment_id: uuid.UUID) -> dict[str, Any]:
        """The revisions and decision basis of what a piece of work is about, now. A task
        reads this before it writes its briefing and hands it back in `Prepared`."""
        async with self._pool.connection() as connection:
            assignment = await (
                await connection.execute(
                    "SELECT * FROM assignments WHERE assignment_id = %s", (assignment_id,)
                )
            ).fetchone()
            if assignment is None:
                raise LookupError(f"there is no assignment {assignment_id}")
            return await _as_it_stands(connection, assignment)

    async def apply(self, invocation_id: uuid.UUID, worker: WorkerSession) -> str:
        """Carry out the effects of a known result, from the first one not yet done.
        Returns `applied`, or why it went no further: `held` (stopped for now),
        `overtaken`, `rejected`, `busy` (one of its effects is being carried out), or
        `superseded` (this try was taken over while it ran)."""
        assert self._effects_for is not None, "nothing says how a result is applied"
        async with self._pool.connection() as connection:
            invocation = await _located(connection, invocation_id)
        effects = self._effects_for(invocation)
        for position, effect in enumerate(effects):
            key, mine = (invocation_id, effect.name), object()
            taken: int | str = "not taken"
            settled = False
            try:
                taken = await self._take(invocation_id, worker, effects, position, mine)
                if isinstance(taken, str):
                    if taken == "done":
                        continue
                    return taken
                run = EffectRun(
                    invocation,
                    f"{invocation_id}:{effect.name}",
                    taken,
                    invocation["applied_under"] or invocation["grant_id"],
                )
                try:
                    detail = await effect.perform(run)
                    marked = await self._mark(invocation_id, effect, run, detail)
                except Overtaken as overtaken:
                    disposed = await self._reject(
                        invocation_id, effect.name, taken, str(overtaken), blameless=True
                    )
                    settled = True
                    return disposed
                except ActionRefused as refused:
                    # By the service, in `perform`, or in `settle`, whose transaction
                    # has been rolled back by now. Settled only once that is on record:
                    # a call cut short while it waits to say so still lets the effect go.
                    disposed = await self._reject(invocation_id, effect.name, taken, str(refused))
                    settled = True
                    return disposed
                settled = True
                if marked != "done":
                    return marked
            finally:
                # Whatever cut this short, and wherever: only the call that took the
                # effect lets it go, in this process and on record. An effect left on
                # record as started by a process that is alive and not carrying it out
                # would be busy to everyone for ever.
                if worker.running.get(key) is mine:
                    if settled:
                        del worker.running[key]
                    else:
                        giving_up = asyncio.create_task(
                            self._give_up(invocation_id, effect.name, worker, mine)
                        )
                        self._giving_up.add(giving_up)
                        giving_up.add_done_callback(self._giving_up.discard)
                        await asyncio.shield(giving_up)
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute(
                "UPDATE invocations SET state = 'applied' WHERE invocation_id = %s "
                "AND state = 'returned'",
                (invocation_id,),
            )
        return "applied"

    async def _give_up(
        self, invocation_id: uuid.UUID, name: str, worker: WorkerSession, mine: object
    ) -> None:
        """This process took the effect and is no longer carrying it out, which it knows
        because the call that took it is the one saying so. If the effect is still on
        record as started by this process, it is free again. One that has been done, or
        taken over by another process since, is not touched. Which try it was does not
        matter, and the call may have been cut short before it learned.

        The call's own claim to the effect in this process is let go only once that is
        on record. Until then the effect is busy to every other call here: one that took
        it in between would have its own try set free by this."""
        try:
            async with self._pool.connection() as connection, connection.transaction():
                await connection.execute(
                    "UPDATE invocation_effects SET state = 'pending' WHERE invocation_id = %s "
                    "AND name = %s AND state = 'started' AND worker_id = %s",
                    (invocation_id, name, worker.worker_id),
                )
        finally:
            if worker.running.get((invocation_id, name)) is mine:
                del worker.running[(invocation_id, name)]

    async def _take(
        self,
        invocation_id: uuid.UUID,
        worker: WorkerSession,
        effects: Sequence[Effect],
        position: int,
        token: object,
    ) -> int | str:
        """Admit one effect: the try number taken, or why it is not to be performed."""
        async with self._pool.connection() as connection, connection.transaction():
            located = await _located(connection, invocation_id)
            assignment = await _locked_assignment(connection, located["assignment_id"])
            invocation = await _locked_invocation(connection, invocation_id)
            if invocation["state"] != "returned":
                return str(invocation["state"])
            for at, each in enumerate(effects):
                await connection.execute(
                    "INSERT INTO invocation_effects (invocation_id, position, name) "
                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (invocation_id, at, each.name),
                )
            effect = await (
                await connection.execute(
                    "SELECT * FROM invocation_effects WHERE invocation_id = %s AND name = %s "
                    "FOR UPDATE",
                    (invocation_id, effects[position].name),
                )
            ).fetchone()
            assert effect is not None
            if effect["state"] == "done":
                return "done"
            key = (invocation_id, str(effect["name"]))
            # Asked first: an effect that is being carried out is its carrier's to
            # dispose of, when it comes to mark it, and nobody else's meanwhile.
            if effect["state"] == "started":
                # Somebody started it. If that was this process, it is being carried out
                # only if this process says so: otherwise it was cut short here. If it
                # was another process, it is being carried out if that process is alive.
                being_carried_out = (
                    key in worker.running
                    if effect["worker_id"] == worker.worker_id
                    else await is_alive(connection, effect["worker_id"])
                )
                if being_carried_out:
                    return "busy"
            ended = await _ended_for_good(connection, assignment, invocation)
            if ended:
                await _overtake(connection, assignment, invocation, ended)
                return "overtaken"
            stopped = await closure_of_feature(
                connection,
                assignment["project_id"],
                assignment["feature_id"],
                assignment["story_id"],
            )
            if stopped is not None:
                # Stopped for now. The result is kept, and nothing is done until the
                # stop is cleared.
                return "held"
            await connection.execute(
                "UPDATE invocation_effects SET state = 'started', try = try + 1, worker_id = %s "
                "WHERE invocation_id = %s AND name = %s",
                (worker.worker_id, invocation_id, effect["name"]),
            )
            # Said before this transaction commits: nobody can see the effect as started
            # by this process and not being carried out by it.
            worker.running[key] = token
            return int(effect["try"]) + 1

    async def _mark(
        self,
        invocation_id: uuid.UUID,
        effect: Effect,
        run: EffectRun,
        detail: Mapping[str, Any] | None,
    ) -> str:
        """Mark the effect done, and write what it writes, if this try may still say so
        and the invocation still authorises it. Returns `done`, or why not: `superseded`
        (the try was taken over), `overtaken` (what the result was about ended or
        changed while the effect ran), `held` (stopped for now: the effect is taken
        again when the stop clears). The locks are taken in the order `_take` takes
        them: the assignment, the invocation, then the effect."""
        async with self._pool.connection() as connection, connection.transaction():
            located = await _located(connection, invocation_id)
            assignment = await _locked_assignment(connection, located["assignment_id"])
            invocation = await _locked_invocation(connection, invocation_id)
            current = await (
                await connection.execute(
                    "SELECT 1 FROM invocation_effects WHERE invocation_id = %s AND name = %s "
                    "AND state = 'started' AND try = %s FOR UPDATE",
                    (invocation_id, effect.name, run.attempt),
                )
            ).fetchone()
            if current is None:
                return "superseded"
            if invocation["state"] != "returned":
                # Someone has disposed of the invocation while this effect ran: it
                # authorises nothing more.
                return str(invocation["state"])
            # Against what was expected before this effect: nothing it does itself is
            # visible yet, since what it changes it changes in `settle`, below.
            ended = await _ended_for_good(connection, assignment, invocation)
            if ended:
                await _overtake(connection, assignment, invocation, ended)
                return "overtaken"
            if (
                await closure_of_feature(
                    connection,
                    assignment["project_id"],
                    assignment["feature_id"],
                    assignment["story_id"],
                )
                is not None
            ):
                # Given up, not cut short: whoever comes when the stop has cleared
                # takes it, without having to find out whether this process is alive.
                await connection.execute(
                    "UPDATE invocation_effects SET state = 'pending' "
                    "WHERE invocation_id = %s AND name = %s",
                    (invocation_id, effect.name),
                )
                return "held"
            await connection.execute(
                "UPDATE invocation_effects SET state = 'done', detail = %s "
                "WHERE invocation_id = %s AND name = %s",
                (Jsonb(dict(detail)) if detail is not None else None, invocation_id, effect.name),
            )
            moved = await effect.settle(connection, run) if effect.settle is not None else None
            if moved:
                # What this effect itself changed is what the next one expects to find.
                await connection.execute(
                    "UPDATE invocations SET expects = expects || %s WHERE invocation_id = %s",
                    (Jsonb(dict(moved)), invocation_id),
                )
            return "done"

    async def _reject(
        self,
        invocation_id: uuid.UUID,
        name: str,
        attempt: int,
        reason: str,
        *,
        blameless: bool = False,
    ) -> str:
        """What the model said was refused by the service it was for. A known call, and
        an attempt: another may be admitted, up to the limit. Only the current try of
        the effect may say this: a try that was taken over says nothing."""
        async with self._pool.connection() as connection, connection.transaction():
            located = await _located(connection, invocation_id)
            assignment = await _locked_assignment(connection, located["assignment_id"])
            invocation = await _locked_invocation(connection, invocation_id)
            if invocation["state"] != "returned":
                return str(invocation["state"])
            current = await (
                await connection.execute(
                    "SELECT 1 FROM invocation_effects WHERE invocation_id = %s AND name = %s "
                    "AND state = 'started' AND try = %s",
                    (invocation_id, name, attempt),
                )
            ).fetchone()
            if current is None:
                return "superseded"
            ended = (
                reason if blameless else await _ended_for_good(connection, assignment, invocation)
            )
            if ended:
                # The work ended or changed while the effect ran. Whatever the service
                # then refused, the reply was not at fault: nobody is asked about it.
                await _overtake(connection, assignment, invocation, ended)
                return "overtaken"
            await connection.execute(
                "UPDATE invocations SET state = 'rejected', reason = %s WHERE invocation_id = %s",
                (reason, invocation_id),
            )
            if await _calls_made(connection, assignment["assignment_id"]) >= ATTEMPTS_BEFORE_ASKING:
                await _park(
                    connection,
                    assignment,
                    invocation_id,
                    f"{ATTEMPTS_BEFORE_ASKING} calls gave nothing that could be used",
                )
            await _note(
                connection, assignment, "invocation.rejected", invocation_id, invocation["attempt"]
            )
            return "rejected"

    async def decide(self, action: HumanAction) -> Receipt:
        """What a person says about work that is parked on one call: `retry` allows one
        more call, once, however often the request is sent; `set_aside` leaves the work
        parked and takes it out of the inbox."""
        require_human(action.principal)
        require_well_formed(action, ON_A_PARKED_CALL)
        try:
            invocation_id = uuid.UUID(action.target)
        except ValueError:
            raise ActionRefused("there is no such call") from None
        async with self._pool.connection() as connection:
            located = await (
                await connection.execute(
                    "SELECT project_id FROM invocations WHERE invocation_id = %s", (invocation_id,)
                )
            ).fetchone()
        if located is None:
            raise ActionRefused("there is no such call")
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, located["project_id"], exclusive=True)
            invocation = await _located(connection, invocation_id)
            assignment = await _locked_assignment(connection, invocation["assignment_id"])
            if (
                seen := answer_again(action, await recorded_receipt(connection, action))
            ) is not None:
                return seen
            invocation = await _locked_invocation(connection, invocation_id)
            latest = await _latest(connection, assignment["assignment_id"])
            assert latest is not None
            # Work that was retired has its park cleared; work that was finished by
            # other means (a roadmap approved over a parked correction) has not.
            if (
                latest["invocation_id"] != invocation_id
                or assignment["parked"] is None
                or assignment["state"] not in ("wanted", "granted")
            ):
                receipt = Receipt("stale", "nothing changed: this work is not parked on that call")
            elif action.action == "retry":
                await connection.execute(
                    "UPDATE invocations SET retry_authorised_by = %s WHERE invocation_id = %s",
                    (action.request_id, invocation_id),
                )
                await _unpark(connection, assignment)
                # Announced again, so that whichever worker is alive takes it up.
                await connection.execute(
                    "UPDATE outbox SET published_at = NULL WHERE message_id = %s",
                    (assignment["command_message_id"],),
                )
                receipt = Receipt("effective", "retry_authorised")
            else:
                await _close_inbox(connection, assignment["assignment_id"])
                receipt = Receipt("effective", "set_aside")
            if receipt.disposition == "effective":
                await append_event(
                    connection,
                    assignment["project_id"],
                    f"invocation.{receipt.outcome}",
                    subject_id=_subject(assignment),
                    payload={
                        "invocation_id": str(invocation_id),
                        "by": action.principal.identity,
                        "request_id": action.request_id,
                    },
                )
            await record_receipt(connection, action, receipt)
            return receipt

    @staticmethod
    async def is_current_try(
        connection: Connection, invocation_id: uuid.UUID, name: str, attempt: int
    ) -> bool:
        """Whether this try of an effect is still the one that may decide, and what its
        invocation is about is still as the invocation expects. Asked inside a
        transaction that is about to accept what the try did: afterwards is too late,
        since what was accepted would stand beside a record saying it was overtaken."""
        row = await (
            await connection.execute(
                "SELECT v.expects, v.assignment_id FROM invocation_effects e "
                "JOIN invocations v USING (invocation_id) "
                "WHERE e.invocation_id = %s AND e.name = %s AND e.state = 'started' "
                "AND e.try = %s AND v.state = 'returned'",
                (invocation_id, name, attempt),
            )
        ).fetchone()
        if row is None:
            return False
        assignment = await (
            await connection.execute(
                "SELECT * FROM assignments WHERE assignment_id = %s", (row["assignment_id"],)
            )
        ).fetchone()
        assert assignment is not None
        now = await _as_it_stands(connection, assignment)
        return all(row["expects"].get(name) == value for name, value in now.items())

    async def last_problem(self, assignment_id: uuid.UUID) -> str | None:
        """Why the last call for this work gave nothing usable, if that is how it ended:
        whoever takes the work up next briefs the model with it, whoever made that call."""
        async with self._pool.connection() as connection:
            latest = await _latest(connection, assignment_id)
        if latest is None or latest["state"] not in ("rejected", "errored"):
            return None
        reason: str | None = latest["reason"]
        return reason

    async def reason_of(self, invocation_id: uuid.UUID) -> str | None:
        """Why a call gave nothing usable, for the briefing of the next."""
        async with self._pool.connection() as connection:
            reason: str | None = (await _located(connection, invocation_id))["reason"]
            return reason

    async def parked(self, project_id: str) -> list[Parked]:
        """The calls a person is being asked about, oldest first."""
        async with self._pool.connection() as connection:
            rows = await (
                await connection.execute(
                    "SELECT v.invocation_id, v.assignment_id, v.role, v.state, a.parked AS reason "
                    "FROM inbox_items i JOIN invocations v USING (invocation_id) "
                    "JOIN assignments a USING (assignment_id) "
                    "WHERE i.project_id = %s AND i.state = 'open' AND i.kind = 'invocation' "
                    "AND a.state IN ('wanted', 'granted') ORDER BY i.opened_at",
                    (project_id,),
                )
            ).fetchall()
        return [Parked(**row) for row in rows]

    async def recover(self, project_id: str) -> None:
        """The pass's part.

        A call in flight whose worker is gone will never be read by anyone: it is
        uncertain, and if the work is still owed a person is asked. A grant whose worker
        is gone and which has no call to show for it is taken back. And every known
        result that is not yet applied is taken up here, whoever obtained it and whether
        or not they are alive: one that was held by a pause is carried on when the pause
        is cleared, and an effect somebody is carrying out is left to them."""
        async with self._pool.connection() as connection:
            in_flight = await (
                await connection.execute(
                    "SELECT invocation_id, assignment_id, worker_id, attempt FROM invocations "
                    "WHERE project_id = %s AND state = 'intended' ORDER BY started_at",
                    (project_id,),
                )
            ).fetchall()
            granted = await (
                await connection.execute(
                    "SELECT assignment_id FROM assignments WHERE project_id = %s "
                    "AND state = 'granted' AND granted_worker IS NOT NULL ORDER BY assignment_id",
                    (project_id,),
                )
            ).fetchall()
        # Each in a transaction of its own, which takes the assignment before it writes
        # to the ledger, as every other transaction about an assignment does, and looks
        # again at what it found.
        for call in in_flight:
            async with self._pool.connection() as connection, connection.transaction():
                assignment = await _locked_assignment(connection, call["assignment_id"])
                still = await _locked_invocation(connection, call["invocation_id"])
                if still["state"] != "intended" or await is_alive(connection, call["worker_id"]):
                    continue
                await connection.execute(
                    "UPDATE invocations SET state = 'uncertain', reason = %s, "
                    "ended_at = clock_timestamp() WHERE invocation_id = %s",
                    ("the worker that made the call is gone", call["invocation_id"]),
                )
                if assignment["state"] in ("wanted", "granted"):
                    await _park(connection, assignment, call["invocation_id"], _UNCERTAIN)
                await _note(
                    connection,
                    assignment,
                    "invocation.uncertain",
                    call["invocation_id"],
                    call["attempt"],
                )
        async with self._pool.connection() as connection:
            known = await (
                await connection.execute(
                    "SELECT invocation_id FROM invocations WHERE project_id = %s "
                    "AND state = 'returned' ORDER BY started_at",
                    (project_id,),
                )
            ).fetchall()
        if known and self._effects_for is not None:
            async with self._starting:
                # One session for this object's recoveries, however many passes ask at once.
                if self._session is None:
                    self._session = await WorkerSession.start(self._pool, "control-plane")
            for each in known:
                await self.apply(each["invocation_id"], self._session)
        # Last, so that a grant whose known result was just found unusable is taken back
        # in this same pass, and not left for the next one.
        for each in granted:
            async with self._pool.connection() as connection, connection.transaction():
                assignment = await _locked_assignment(connection, each["assignment_id"])
                if (
                    assignment["state"] != "granted"
                    or assignment["granted_worker"] is None
                    or await is_alive(connection, assignment["granted_worker"])
                ):
                    continue
                # What this grant has to show for itself: a grant that admitted no call
                # of its own, or whose last call gave nothing, is taken back, whatever
                # earlier grants of the same work left. A person's permission to retry
                # is on the work, and stays.
                own = await (
                    await connection.execute(
                        "SELECT state FROM invocations WHERE assignment_id = %s AND "
                        "(grant_id = %s OR applied_under = %s) ORDER BY attempt DESC LIMIT 1",
                        (
                            assignment["assignment_id"],
                            assignment["grant_id"],
                            assignment["grant_id"],
                        ),
                    )
                ).fetchone()
                if own is None or own["state"] in ("errored", "rejected", "not_started"):
                    await _release(connection, assignment)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.stop()
            self._session = None


async def _located(connection: Connection, invocation_id: uuid.UUID) -> dict[str, Any]:
    row = await (
        await connection.execute(
            # With the kind of work it is a call for: how a result is applied goes by it.
            "SELECT v.*, a.kind FROM invocations v JOIN assignments a USING (assignment_id) "
            "WHERE v.invocation_id = %s",
            (invocation_id,),
        )
    ).fetchone()
    if row is None:
        raise LookupError(f"there is no call {invocation_id}")
    return row


async def _locked_invocation(connection: Connection, invocation_id: uuid.UUID) -> dict[str, Any]:
    row = await (
        await connection.execute(
            "SELECT * FROM invocations WHERE invocation_id = %s FOR UPDATE", (invocation_id,)
        )
    ).fetchone()
    assert row is not None
    return row


async def _locked_assignment(connection: Connection, assignment_id: uuid.UUID) -> dict[str, Any]:
    """Gate, then the story if the work is a story's own, then the behaviour if it has
    one, then the assignment: the order `reopen_red` takes them in, which retires a
    build under its behaviour's lock, and the order every transition of a story takes.

    Work that is applied by proposing a roadmap, by changing a story's list, or by
    putting a question, does that in the transaction that marks its effect, and each of
    those needs the gate exclusively. It is taken so from the start: a transaction that
    held it shared and then asked for more would wait on another doing the same."""
    known = await (
        await connection.execute(
            "SELECT project_id, kind, story_id, behaviour_id FROM assignments "
            "WHERE assignment_id = %s",
            (assignment_id,),
        )
    ).fetchone()
    if known is None:
        raise LookupError(f"there is no assignment {assignment_id}")
    await lock_gate(
        connection, known["project_id"], exclusive=known["kind"] not in (BUILD, REMEDIATE, REVIEW)
    )
    if known["story_id"] is not None and known["behaviour_id"] is None:
        await connection.execute(
            "SELECT 1 FROM stories WHERE project_id = %s AND story_id = %s FOR UPDATE",
            (known["project_id"], known["story_id"]),
        )
    if known["behaviour_id"] is not None:
        await connection.execute(
            "SELECT 1 FROM behaviours WHERE project_id = %s AND behaviour_id = %s FOR UPDATE",
            (known["project_id"], known["behaviour_id"]),
        )
    row = await (
        await connection.execute(
            "SELECT * FROM assignments WHERE assignment_id = %s FOR UPDATE", (assignment_id,)
        )
    ).fetchone()
    assert row is not None
    return row


async def _latest(connection: Connection, assignment_id: uuid.UUID) -> dict[str, Any] | None:
    return await (
        await connection.execute(
            "SELECT * FROM invocations WHERE assignment_id = %s ORDER BY attempt DESC LIMIT 1",
            (assignment_id,),
        )
    ).fetchone()


async def _calls_made(connection: Connection, assignment_id: uuid.UUID) -> int:
    """Counted over the assignment, across every grant it has had: taking a grant back
    and giving it again buys no further attempts."""
    row = await (
        await connection.execute(
            "SELECT count(*) AS made FROM invocations WHERE assignment_id = %s "
            "AND state <> 'not_started'",
            (assignment_id,),
        )
    ).fetchone()
    assert row is not None
    return int(row["made"])


async def _has_a_successor(connection: Connection, invocation: dict[str, Any]) -> bool:
    row = await (
        await connection.execute(
            "SELECT 1 FROM invocations WHERE assignment_id = %s AND attempt > %s LIMIT 1",
            (invocation["assignment_id"], invocation["attempt"]),
        )
    ).fetchone()
    return row is not None


async def _require_may_start(connection: Connection, assignment: dict[str, Any]) -> None:
    if assignment["kind"] not in (RECONFIRM, CORRECT) and assignment["story_id"] is not None:
        await require_startable(
            connection, assignment["project_id"], assignment["story_id"], assignment["behaviour_id"]
        )
    require_open(
        await closure_of_feature(
            connection, assignment["project_id"], assignment["feature_id"], assignment["story_id"]
        )
    )


async def _as_it_stands(connection: Connection, assignment: dict[str, Any]) -> dict[str, Any]:
    """The revisions and the decision basis of what the work is about, now."""
    if assignment["story_id"] is None:
        return {
            "basis": await feature_basis(
                connection, assignment["project_id"], assignment["feature_id"]
            )
        }
    story = await (
        await connection.execute(
            "SELECT revision, list_revision FROM stories WHERE project_id = %s AND story_id = %s",
            (assignment["project_id"], assignment["story_id"]),
        )
    ).fetchone()
    assert story is not None
    return {
        "story_revision": story["revision"],
        "list_revision": story["list_revision"],
        "basis": await story_basis(connection, assignment["project_id"], assignment["story_id"]),
    }


async def _ended_for_good(
    connection: Connection, assignment: dict[str, Any], invocation: dict[str, Any]
) -> str:
    """Why the result will never be applied, or empty if it still may be."""
    # Work that is ended for good (a cancel, a rejection, a reopened red) is retired by
    # whatever ended it.
    if assignment["state"] == "retired":
        return "the work it was for was ended"
    under = invocation["applied_under"] or invocation["grant_id"]
    if assignment["state"] in ("done", "asked") and assignment["grant_id"] != under:
        # Finished, and not under the grant this result acts under: by a roadmap approved
        # meanwhile, say. Work finished under that grant was finished by this apply, or by
        # the same hands, and the rest of it goes on.
        return "the work it was for was finished by other means"
    if assignment["state"] in ("wanted", "granted") and assignment["grant_id"] != under:
        return "the grant it was to be applied under no longer holds the work"
    # Is it still about what there is? What it expects moves only by what its own
    # effects said they changed, so a decision made between two of them is seen.
    now = await _as_it_stands(connection, assignment)
    expected = invocation["expects"]
    if any(expected.get(name) != value for name, value in now.items()):
        return "what it was about has changed since the call was admitted"
    return ""


async def _overtake(
    connection: Connection, assignment: dict[str, Any], invocation: dict[str, Any], reason: str
) -> None:
    """The result will be applied to nothing, through no fault of the reply.

    If the work is still owed, and is held by the grant this result acted under, it is
    not left there: whoever holds that grant has been answered, and nobody would come
    back for it. It is wanted again and announced again. When as many calls have been
    made for it as a person is asked after, it is parked instead: being nobody's fault
    does not make a fourth paid call free."""
    await connection.execute(
        "UPDATE invocations SET state = 'overtaken', reason = %s WHERE invocation_id = %s",
        (reason, invocation["invocation_id"]),
    )
    await _note(
        connection,
        assignment,
        "invocation.overtaken",
        invocation["invocation_id"],
        invocation["attempt"],
    )
    under = invocation["applied_under"] or invocation["grant_id"]
    if assignment["state"] != "granted" or assignment["grant_id"] != under:
        return
    if await _calls_made(connection, assignment["assignment_id"]) >= ATTEMPTS_BEFORE_ASKING:
        await _park(
            connection,
            assignment,
            invocation["invocation_id"],
            f"{ATTEMPTS_BEFORE_ASKING} calls gave nothing that could be used",
        )
        return
    await _release(connection, assignment)
    await connection.execute(
        "UPDATE outbox SET published_at = NULL WHERE message_id = %s",
        (assignment["command_message_id"],),
    )


async def _release(connection: Connection, assignment: dict[str, Any]) -> None:
    """Take the grant back: the work is wanted again, by whoever is alive to do it."""
    if assignment["state"] != "granted":
        return
    await connection.execute(
        "UPDATE assignments SET state = 'wanted', grant_id = NULL, granted_to = NULL, "
        "granted_worker = NULL, first_effect_at = NULL WHERE assignment_id = %s",
        (assignment["assignment_id"],),
    )
    await append_event(
        connection,
        assignment["project_id"],
        "assignment.grant_revoked",
        subject_id=_subject(assignment),
        payload={
            "assignment_id": str(assignment["assignment_id"]),
            "grant_id": str(assignment["grant_id"]),
            "worker": assignment["granted_to"],
        },
    )


async def _park(
    connection: Connection, assignment: dict[str, Any], invocation_id: uuid.UUID, why: str
) -> None:
    """Nothing more is called for this work until a person says so. The grant is taken
    back, so that whoever is alive then can take the work up."""
    await connection.execute(
        "UPDATE assignments SET parked = %s WHERE assignment_id = %s",
        (why, assignment["assignment_id"]),
    )
    await _release(connection, assignment)
    await connection.execute(
        "INSERT INTO inbox_items (item_id, project_id, kind, invocation_id) "
        "VALUES (%s, %s, 'invocation', %s)",
        (uuid.uuid7(), assignment["project_id"], invocation_id),
    )


async def _unpark(connection: Connection, assignment: dict[str, Any]) -> None:
    await connection.execute(
        "UPDATE assignments SET parked = NULL WHERE assignment_id = %s",
        (assignment["assignment_id"],),
    )
    await _close_inbox(connection, assignment["assignment_id"])


async def _close_inbox(connection: Connection, assignment_id: uuid.UUID) -> None:
    await connection.execute(
        "UPDATE inbox_items SET state = 'closed' WHERE state = 'open' AND invocation_id IN "
        "(SELECT invocation_id FROM invocations WHERE assignment_id = %s)",
        (assignment_id,),
    )


async def _note(
    connection: Connection,
    assignment: dict[str, Any],
    event: str,
    invocation_id: uuid.UUID,
    attempt: int,
) -> None:
    await append_event(
        connection,
        assignment["project_id"],
        event,
        subject_id=_subject(assignment),
        payload={
            "invocation_id": str(invocation_id),
            "assignment_id": str(assignment["assignment_id"]),
            "attempt": attempt,
        },
    )


def _subject(assignment: dict[str, Any]) -> str:
    return str(assignment["behaviour_id"] or assignment["story_id"] or assignment["feature_id"])


def reply_problem(result: ProviderResult, schema: Mapping[str, Any]) -> str | None:
    """Why what a call returned is not the structured answer it was asked for, or None
    if it is, or if the call returned nothing to check. Asking the provider to enforce a
    schema is not its having done so."""
    if result.outcome is not Outcome.RETURNED:
        return None
    reply = result.terminal.structured_output if result.terminal is not None else None
    if reply is None:
        return "the reply did not carry the structured answer it was asked for"
    return schema_problem(reply, dict(schema))


def _ending(result: ProviderResult, problem: str | None) -> tuple[str, str | None]:
    if result.outcome is Outcome.RETURNED:
        return ("rejected", problem) if problem else ("returned", None)
    if result.outcome is Outcome.ERRORED:
        assert result.terminal is not None
        return "errored", result.terminal.subtype or result.terminal.terminal_reason or "an error"
    if result.outcome is Outcome.NOT_STARTED:
        return "not_started", result.reason
    return "uncertain", result.reason


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(value), sort_keys=True).encode()).hexdigest()
