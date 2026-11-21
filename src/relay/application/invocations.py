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

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from relay.application.assignments import CORRECT, RECONFIRM
from relay.application.closure import (
    CANCELLED,
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


@dataclass(frozen=True)
class Admission:
    """`admitted`: call the provider under this invocation. `apply`: a result is already
    known; apply it, call nothing. `finished`: nothing is left to do for this
    assignment. `refused`: nothing may be called now, for the reason given."""

    outcome: str
    invocation_id: uuid.UUID | None = None
    attempt: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class EffectRun:
    """What an effect is given: the invocation it is an effect of, and the identity of
    this try of it. An effect names whatever it starts by that identity."""

    invocation: dict[str, Any]
    identity: str


@dataclass(frozen=True)
class Effect:
    """One thing that applying a result does. It must be safe to perform again under
    the same identity: it is performed again if the process stops before it is marked."""

    name: str
    perform: Callable[[EffectRun], Awaitable[Mapping[str, Any] | None]]


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

    async def admit(
        self,
        worker: WorkerSession,
        assignment_id: uuid.UUID,
        grant_id: uuid.UUID,
        prepared: Prepared,
    ) -> Admission:
        """Decide, under the assignment's lock, whether a provider may be called now."""
        async with self._pool.connection() as connection, connection.transaction():
            assignment = await _locked_assignment(connection, assignment_id)
            if assignment["state"] in ("done", "retired"):
                return Admission("finished")
            if assignment["state"] != "granted" or assignment["grant_id"] != grant_id:
                return Admission("refused", reason="this grant is not the current one")
            latest = await _latest(connection, assignment_id)
            retrying = False
            if latest is not None:
                state = latest["state"]
                if state == "returned":
                    return Admission("apply", latest["invocation_id"], latest["attempt"])
                if state == "intended":
                    return Admission("refused", reason="a call for this work is in flight")
                if state in ("applied", "overtaken"):
                    return Admission("finished")
                # Looked at before whether the work is parked: an authorised retry is
                # what unparks it.
                retrying = (
                    latest["retry_authorised_by"] is not None
                    and latest["retry_consumed_by"] is None
                )
                if state == "uncertain" and not retrying:
                    return Admission("refused", reason=f"parked: {_UNCERTAIN}")
            if assignment["parked"] is not None and not retrying:
                return Admission("refused", reason=f"parked: {assignment['parked']}")
            try:
                # Every stop that would refuse a claim refuses a paid call, the first
                # and every one after it.
                await _require_may_start(connection, assignment)
            except ActionRefused as stopped:
                return Admission("refused", reason=str(stopped))
            invocation_id = uuid.uuid7()
            attempt = (latest["attempt"] if latest is not None else 0) + 1
            admitted_on = {
                **await _as_it_stands(connection, assignment),
                "grant_id": str(grant_id),
                "attempt": attempt,
            }
            await connection.execute(
                "INSERT INTO invocations (invocation_id, assignment_id, project_id, grant_id, "
                "attempt, worker_id, role, provider, model, effort, agent_name, agent_path, "
                "agent_sha256, agent_revision, agent_dirty, effective_agent_sha256, briefing, "
                "briefing_sha256, schema_sha256, admitted_on) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
                ),
            )
            if retrying:
                assert latest is not None
                await connection.execute(
                    "UPDATE invocations SET retry_consumed_by = %s WHERE invocation_id = %s",
                    (invocation_id, latest["invocation_id"]),
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
            state, reason = _ending(result, problem)
            if late and result.terminal is None:
                return "uncertain"  # nothing more is known than was
            if late and await _has_a_successor(connection, invocation):
                # Another call was admitted in its place. What this one returned is kept
                # as evidence, with what it used, and applied to nothing.
                state, reason = "overtaken", "its result arrived after another call was admitted"
            usage, observed, estimated = self._usage(result)
            await connection.execute(
                "UPDATE invocations SET state = %s, reason = %s, envelope = %s, reply = %s, "
                "retained = %s, usage = %s, provider_cost_usd = %s, estimated_cost_usd = %s, "
                "price_table = %s, retry_authorised_by = NULL, ended_at = clock_timestamp() "
                "WHERE invocation_id = %s",
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
                    invocation_id,
                ),
            )
            if late and state != "overtaken":
                # The result is known after all: the work is no longer waiting on a
                # person, and if nobody has taken it up since, it is this call's again.
                await _unpark(connection, assignment)
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
            if state == "not_started":
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

    async def apply(self, invocation_id: uuid.UUID, worker: WorkerSession) -> str:
        """Carry out the effects of a known result, from the first one not yet done.
        Returns `applied`, or why it went no further: `held` (stopped for now),
        `overtaken`, `rejected`, `busy` (another live worker is carrying one out), or
        `superseded` (this try was taken over while it ran)."""
        assert self._effects_for is not None, "nothing says how a result is applied"
        async with self._pool.connection() as connection:
            invocation = await _located(connection, invocation_id)
        effects = self._effects_for(invocation)
        for position, effect in enumerate(effects):
            taken = await self._take(invocation_id, worker, effects, position)
            if isinstance(taken, str):
                if taken == "done":
                    continue
                return taken
            try:
                detail = await effect.perform(
                    EffectRun(invocation, f"{invocation_id}:{effect.name}:{taken}")
                )
            except ActionRefused as refused:
                return await self._reject(invocation_id, str(refused))
            if not await self._mark(invocation_id, effect.name, taken, detail):
                return "superseded"
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute(
                "UPDATE invocations SET state = 'applied' WHERE invocation_id = %s "
                "AND state = 'returned'",
                (invocation_id,),
            )
        return "applied"

    async def _take(
        self,
        invocation_id: uuid.UUID,
        worker: WorkerSession,
        effects: Sequence[Effect],
        position: int,
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
            ended = await _ended_for_good(connection, assignment, invocation, first=position == 0)
            if ended:
                await connection.execute(
                    "UPDATE invocations SET state = 'overtaken', reason = %s "
                    "WHERE invocation_id = %s",
                    (ended, invocation_id),
                )
                await _note(
                    connection,
                    assignment,
                    "invocation.overtaken",
                    invocation_id,
                    invocation["attempt"],
                )
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
            if (
                effect["state"] == "started"
                and effect["worker_id"] != worker.worker_id
                and await is_alive(connection, effect["worker_id"])
            ):
                return "busy"
            await connection.execute(
                "UPDATE invocation_effects SET state = 'started', try = try + 1, worker_id = %s "
                "WHERE invocation_id = %s AND name = %s",
                (worker.worker_id, invocation_id, effect["name"]),
            )
            return int(effect["try"]) + 1

    async def _mark(
        self, invocation_id: uuid.UUID, name: str, attempt: int, detail: Mapping[str, Any] | None
    ) -> bool:
        """The effect is done, if this try is still the one that may say so. A try that
        was taken over while it ran decides nothing."""
        async with self._pool.connection() as connection, connection.transaction():
            marked = await (
                await connection.execute(
                    "UPDATE invocation_effects SET state = 'done', detail = %s "
                    "WHERE invocation_id = %s AND name = %s AND state = 'started' AND try = %s "
                    "RETURNING name",
                    (
                        Jsonb(dict(detail)) if detail is not None else None,
                        invocation_id,
                        name,
                        attempt,
                    ),
                )
            ).fetchone()
            return marked is not None

    async def _reject(self, invocation_id: uuid.UUID, reason: str) -> str:
        """What the model said was refused by the service it was for. A known call, and
        an attempt: another may be admitted, up to the limit."""
        async with self._pool.connection() as connection, connection.transaction():
            located = await _located(connection, invocation_id)
            assignment = await _locked_assignment(connection, located["assignment_id"])
            invocation = await _locked_invocation(connection, invocation_id)
            if invocation["state"] != "returned":
                return str(invocation["state"])
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
            if latest["invocation_id"] != invocation_id or assignment["parked"] is None:
                receipt = Receipt("stale", "nothing changed: this work is not parked on that call")
            elif action.action == "retry":
                await connection.execute(
                    "UPDATE invocations SET retry_authorised_by = %s, retry_consumed_by = NULL "
                    "WHERE invocation_id = %s",
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

    async def parked(self, project_id: str) -> list[Parked]:
        """The calls a person is being asked about, oldest first."""
        async with self._pool.connection() as connection:
            rows = await (
                await connection.execute(
                    "SELECT v.invocation_id, v.assignment_id, v.role, v.state, a.parked AS reason "
                    "FROM inbox_items i JOIN invocations v USING (invocation_id) "
                    "JOIN assignments a USING (assignment_id) "
                    "WHERE i.project_id = %s AND i.state = 'open' AND i.kind = 'invocation' "
                    "ORDER BY i.opened_at",
                    (project_id,),
                )
            ).fetchall()
        return [Parked(**row) for row in rows]

    async def recover(self, project_id: str) -> None:
        """The pass's part. A grant whose worker is gone is dealt with by what its
        latest call left: nothing (the work is wanted again), a call in flight (nobody
        will ever read its result: a person is asked), a known result (applied here,
        under the grant it was obtained under)."""
        to_apply: list[uuid.UUID] = []
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
            orphaned = await (
                await connection.execute(
                    "SELECT * FROM assignments WHERE project_id = %s AND state = 'granted' "
                    "AND granted_worker IS NOT NULL ORDER BY assignment_id FOR UPDATE",
                    (project_id,),
                )
            ).fetchall()
            for assignment in orphaned:
                if await is_alive(connection, assignment["granted_worker"]):
                    continue
                latest = await _latest(connection, assignment["assignment_id"])
                if latest is None or latest["state"] in ("errored", "rejected", "not_started"):
                    await _release(connection, assignment)
                elif latest["state"] == "intended":
                    await connection.execute(
                        "UPDATE invocations SET state = 'uncertain', reason = %s, "
                        "ended_at = clock_timestamp() WHERE invocation_id = %s",
                        ("the worker that made the call is gone", latest["invocation_id"]),
                    )
                    await _park(connection, assignment, latest["invocation_id"], _UNCERTAIN)
                    await _note(
                        connection,
                        assignment,
                        "invocation.uncertain",
                        latest["invocation_id"],
                        latest["attempt"],
                    )
                elif latest["state"] == "returned":
                    to_apply.append(latest["invocation_id"])
        if to_apply and self._effects_for is not None:
            if self._session is None:
                self._session = await WorkerSession.start(self._pool, "control-plane")
            for invocation_id in to_apply:
                await self.apply(invocation_id, self._session)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.stop()
            self._session = None


async def _located(connection: Connection, invocation_id: uuid.UUID) -> dict[str, Any]:
    row = await (
        await connection.execute(
            "SELECT * FROM invocations WHERE invocation_id = %s", (invocation_id,)
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
    """Gate, then the behaviour if the work has one, then the assignment: the order
    `reopen_red` takes them in, which retires a build under its behaviour's lock."""
    known = await (
        await connection.execute(
            "SELECT project_id, behaviour_id FROM assignments WHERE assignment_id = %s",
            (assignment_id,),
        )
    ).fetchone()
    if known is None:
        raise LookupError(f"there is no assignment {assignment_id}")
    await lock_gate(connection, known["project_id"])
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
    connection: Connection, assignment: dict[str, Any], invocation: dict[str, Any], *, first: bool
) -> str:
    """Why the result will never be applied, or empty if it still may be."""
    if assignment["state"] == "retired":
        return "the work it was for is no longer wanted"
    stopped = await closure_of_feature(
        connection, assignment["project_id"], assignment["feature_id"], assignment["story_id"]
    )
    if stopped == CANCELLED:
        return "the work it was for was ended"
    if first:
        # Before anything has been done with it: is it still about what there is? Its
        # own later effects may move these, so they are compared only here.
        now = await _as_it_stands(connection, assignment)
        was = invocation["admitted_on"]
        if any(was.get(name) != value for name, value in now.items()):
            return "what it was about has changed since the call was admitted"
    return ""


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
