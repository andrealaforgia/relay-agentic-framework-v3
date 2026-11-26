"""What a worker does with one piece of work: claim it, and then, as often as it is
allowed, call the provider, record what came back, and apply it.

The worker is trusted code. The model is not: it is handed a briefing, it replies, and
everything that happens to Relay's state happens here, from the reply. The model is
given no way to call Relay.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from relay.application.assignments import Assignments
from relay.application.invocations import Invocations, Prepared, reply_problem
from relay.application.workers import WorkerSession
from relay.domain.review import SubmissionRefused
from relay.domain.story import ActionRefused
from relay.persistence.database import Connection
from relay.providers.boundary_suite import UNATTESTED
from relay.providers.claude import (
    Confinement,
    Outcome,
    ProviderResult,
    ProviderUnavailable,
    Request,
)
from relay.providers.confinement import boundary_digest
from relay.transport.streams import Streams

# How often a call is prepared again because what it was prepared from moved.
_PREPARATIONS = 3


@dataclass(frozen=True)
class Call:
    """A call a task wants made: what is recorded of it before it is admitted, and
    where and within what bounds it runs."""

    prepared: Prepared
    workdir: Path
    confinement: Confinement
    timeout: float
    budget_usd: Decimal | None = None


class Task(Protocol):
    """One kind of work, for one role."""

    async def prepare(
        self, assignment: Mapping[str, Any], grant: uuid.UUID, last_problem: str | None
    ) -> Call:
        """The call to make, under this grant. `last_problem` is why the last reply
        could not be used."""
        ...

    def problem_with(self, reply: Any) -> str | None:
        """Why a reply that arrived cannot be used, or None if it can."""
        ...

    async def retain(
        self, assignment: Mapping[str, Any], call: Call, invocation_id: uuid.UUID
    ) -> str | None:
        """Make durable whatever the call left outside its reply, before the reply is
        recorded as usable, and name it: for a build, the commit its workspace was kept
        as. None for work whose reply is all there is."""
        ...

    def discard(self, call: Call) -> None:
        """Let go of what was set up for a call that is finished with, whatever became
        of it: the tree it was to run in."""
        ...


class Provider(Protocol):
    async def invoke(
        self,
        request: Request,
        *,
        on_result: Callable[[ProviderResult], None] | None = None,
        on_start: Callable[[int], None] | None = None,
    ) -> ProviderResult:
        """`on_start` is called when the provider has launched the call, and not before;
        `on_result` when it knows how the call ended, as it classified it."""
        ...


class Shown(Protocol):
    async def authorising(self, connection: Connection, digest: str) -> uuid.UUID | None:
        """The assessment that allows a launch within this boundary now, or None if it
        has not been shown to hold on this host, recently. Asked on the connection of
        the transaction that admits the call."""
        ...


class Worker:
    def __init__(
        self,
        session: WorkerSession,
        claims: Assignments,
        invocations: Invocations,
        provider: Provider,
        tasks: Mapping[str, Task],
        boundaries: Shown,
        runtime: Callable[[], Mapping[str, str]],
        then: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        # What the control plane does by itself once something has been applied: the
        # pass, which publishes a candidate, observes a red, takes up an integration.
        self._then = then
        self._session = session
        self._claims = claims
        self._invocations = invocations
        self._provider = provider
        self._tasks = tasks
        self._boundaries = boundaries
        # What the provider's CLI and this host are, as a pass of a boundary names them.
        # Asked at every launch: a policy file that changed since the worker started is
        # another runtime, with no pass.
        self._runtime = runtime
        self._recording: set[asyncio.Task[str]] = set()

    async def serve_once(
        self, streams: Streams, project_id: str, *, abandoned_after_ms: int = 300_000
    ) -> list[str]:
        """Read this role's commands for a project and carry out what they announce.
        Returns where each was left.

        The stream is only the announcement. What is owed is in PostgreSQL, and the pass
        announces again whatever is still wanted. So a command is acknowledged once this
        worker has dealt with it, whatever came of it; one whose worker failed part-way
        stays unacknowledged, and is taken over by whichever worker reads next after it
        has lain long enough."""
        stream = f"relay:{project_id}:cmd:{self._session.role}"
        group, consumer = self._session.role, str(self._session.worker_id)
        deliveries = [
            *await streams.claim_abandoned(
                stream, group=group, consumer=consumer, idle_for_ms=abandoned_after_ms
            ),
            *await streams.read(stream, group=group, consumer=consumer),
        ]
        left = []
        for delivery in deliveries:
            left.append(await self._serve(delivery.envelope))
            await streams.acknowledge(stream, group, delivery)
        return left

    async def _serve(self, command: Mapping[str, Any]) -> str:
        try:
            assignment_id = uuid.UUID(str(command.get("payload", {}).get("assignment_id")))
        except ValueError:
            return "not a command for work"
        assignment = await self._claims.of(assignment_id)
        if assignment is None or assignment["kind"] not in self._tasks:
            return "not work this worker does"
        return await self.carry_out(assignment)

    async def carry_out(self, assignment: Mapping[str, Any]) -> str:
        """Do what can be done for this work now, and say where that left it: `applied`,
        or the state its last call ended in, or `refused: ...` if nothing could start.
        What was applied may leave something for the control plane to do by itself, and
        that is done before this returns."""
        left = await self._carry_out(assignment)
        if left == "applied" and self._then is not None:
            await self._then(str(assignment["project_id"]))
        return left

    async def _carry_out(self, assignment: Mapping[str, Any]) -> str:
        assignment_id: uuid.UUID = assignment["assignment_id"]
        task = self._tasks[assignment["kind"]]
        try:
            grant = await self._claims.claim(
                self._session.principal, assignment_id, self._session.worker_id
            )
        except ActionRefused as refused:
            return f"refused: {refused}"
        # A call for this work may already have given nothing, under this grant or an
        # earlier one: the next briefing says what was wrong with it.
        problem = await self._invocations.last_problem(assignment_id)
        stale = 0
        prepared: list[Call] = []
        try:
            while True:
                try:
                    call = await task.prepare(assignment, grant, problem)
                except BaseException as failed:
                    # Nothing will be done under this grant by a worker that could not
                    # so much as prepare the call, or was stopped while it did. The grant
                    # is handed back, if it owns nothing, so that the work is not left
                    # with a worker that is alive and not doing it.
                    await asyncio.shield(self._claims.give_back(assignment_id, grant))
                    if isinstance(failed, ActionRefused | SubmissionRefused):
                        return f"refused: {failed}"
                    raise
                prepared.append(call)
                runtime = self._runtime()
                if runtime.get("remote_policy", UNATTESTED) == UNATTESTED:
                    # Part of what bounds a run is policy this host may be given from
                    # elsewhere, which Relay cannot read. Until the operator has stated
                    # what it is, nothing is launched.
                    await self._claims.give_back(assignment_id, grant)
                    return (
                        "refused: nobody has stated what policy this host is given from elsewhere"
                    )
                boundary = boundary_digest(call.confinement, runtime)

                async def authorised(
                    connection: Connection, boundary: str = boundary
                ) -> uuid.UUID | None:
                    # No call is made within bounds nobody has seen hold here: not on a
                    # new version of the CLI, not under changed settings, not after a
                    # pass has gone stale or a later run of the suite has failed. Asked
                    # where the call is admitted, after admission has its locks.
                    return await self._boundaries.authorising(connection, boundary)

                admission = await self._invocations.admit(
                    self._session, assignment_id, grant, call.prepared, authorised=authorised
                )
                if admission.outcome == "stale" and stale < _PREPARATIONS:
                    # What the briefing was written from moved while it was being written.
                    stale += 1
                    continue
                if admission.outcome in ("refused", "stale"):
                    # Nothing will be done under this grant now. If it owns nothing, it
                    # is handed back, so that the work can be taken up again when
                    # whatever refused it has cleared.
                    await self._claims.give_back(assignment_id, grant)
                    return f"refused: {admission.reason}"
                if admission.outcome == "finished":
                    return "finished"
                assert admission.invocation_id is not None
                if admission.outcome == "apply":
                    # A result is already known for this work. Nothing is called.
                    return await self._invocations.apply(admission.invocation_id, self._session)
                state = await self._call(admission.invocation_id, task, assignment, call)
                if state == "returned":
                    state = await self._invocations.apply(admission.invocation_id, self._session)
                    if state != "rejected":
                        return state
                    problem = await self._invocations.reason_of(admission.invocation_id)
                elif state in ("errored", "rejected"):
                    problem = await self._invocations.reason_of(admission.invocation_id)
                else:
                    return state
                # The call gave nothing usable. Whether another is allowed is admission's
                # to say, and the next briefing says what was wrong with this one.
        finally:
            self._let_go(task, prepared)

    def _let_go(self, task: Task, calls: list[Call]) -> None:
        """Discard what was set up for these calls, once nothing is still being kept
        from it: a record being made for a caller that was cancelled may be keeping a
        build's workspace as a commit at this moment."""
        recording = set(self._recording)

        def discard(_: object = None) -> None:
            if all(each.done() for each in recording):
                for call in calls:
                    task.discard(call)
                calls.clear()

        for each in recording:
            each.add_done_callback(discard)
        discard()

    async def _call(
        self, invocation_id: uuid.UUID, task: Task, assignment: Mapping[str, Any], call: Call
    ) -> str:
        """Call the provider and record what came of it. Whatever happens from the call
        being made to its record being durable, what is known of the call is recorded:
        that it never started, that it ended with this result, or that nobody can say.
        Returns the state the call was recorded in."""
        heard: list[ProviderResult] = []
        launched: list[int] = []
        try:
            result = await self._provider.invoke(
                Request(
                    invocation_id=invocation_id,
                    definition=call.prepared.definition,
                    model=call.prepared.model,
                    effort=call.prepared.effort,
                    briefing=call.prepared.briefing,
                    schema=call.prepared.schema,
                    workdir=call.workdir,
                    confinement=call.confinement,
                    timeout=call.timeout,
                    budget_usd=call.budget_usd,
                ),
                on_result=heard.append,
                on_start=launched.append,
            )
        except ProviderUnavailable as unavailable:
            # Established before anything was launched: nothing was spent.
            result = ProviderResult(Outcome.NOT_STARTED, reason=str(unavailable))
        except BaseException as cut_short:
            # Cancelled, or failed in a way nobody planned for, after the call may have
            # been launched. If the provider had already said how it ended, that is its
            # result, as the provider classified it.
            if heard:
                known = heard[-1]
            elif launched:
                known = ProviderResult(
                    Outcome.UNCERTAIN, reason=f"the call was cut short: {cut_short!r}"
                )
            else:
                # Cut short before the provider said it had launched anything: nothing
                # was spent, and nobody needs asking.
                known = ProviderResult(
                    Outcome.NOT_STARTED, reason=f"cut short before the call began: {cut_short!r}"
                )
            await self._recorded(invocation_id, task, assignment, call, known)
            raise
        return await self._recorded(invocation_id, task, assignment, call, result)

    async def _recorded(
        self,
        invocation_id: uuid.UUID,
        task: Task,
        assignment: Mapping[str, Any],
        call: Call,
        result: ProviderResult,
    ) -> str:
        """Record, by a task of the worker's own: a caller that is cancelled while the
        record is being made does not take the record with it."""
        recording = asyncio.create_task(self._record(invocation_id, task, assignment, call, result))
        self._recording.add(recording)
        recording.add_done_callback(self._recording.discard)
        return await asyncio.shield(recording)

    async def _record(
        self,
        invocation_id: uuid.UUID,
        task: Task,
        assignment: Mapping[str, Any],
        call: Call,
        result: ProviderResult,
    ) -> str:
        try:
            problem = _problem(task, result, call.prepared.schema)
        except Exception as fault:  # noqa: BLE001 - a fault in the check loses no result
            # The reply arrived and the check of it failed, which is not the same as the
            # reply failing the check. Either way the call and what it used are recorded.
            problem = f"the reply could not be checked: {fault!r}"
        retained = None
        if result.outcome is Outcome.RETURNED and problem is None and not result.unusable:
            try:
                retained = await task.retain(assignment, call, invocation_id)
            except Exception as failure:  # noqa: BLE001 - whatever stopped it is recorded
                # The reply arrived and what it left could not be kept. That is known,
                # and is recorded with what the call used.
                problem = f"what the call left could not be kept: {failure}"
        return await self._invocations.record(
            invocation_id, result, problem=problem, retained=retained
        )


def _problem(task: Task, result: ProviderResult, schema: Mapping[str, Any]) -> str | None:
    if result.outcome is not Outcome.RETURNED:
        return None
    # Against the schema of the call, before the task reads any of the reply. The record
    # makes the same check against the schema it stored, whoever brings it a result.
    found = reply_problem(result, schema)
    if found is not None or result.terminal is None:
        return found
    return task.problem_with(result.terminal.structured_output)
