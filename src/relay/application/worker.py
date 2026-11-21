"""What a worker does with one piece of work: claim it, and then, as often as it is
allowed, call the provider, record what came back, and apply it.

The worker is trusted code. The model is not: it is handed a briefing, it replies, and
everything that happens to Relay's state happens here, from the reply. The model is
given no way to call Relay.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from relay.application.assignments import Assignments
from relay.application.invocations import Invocations, Prepared
from relay.application.workers import WorkerSession
from relay.domain.story import ActionRefused
from relay.providers.claude import (
    Confinement,
    Outcome,
    ProviderResult,
    ProviderUnavailable,
    Request,
)
from relay.providers.stream import Event, Terminal

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

    async def prepare(self, assignment: Mapping[str, Any], last_problem: str | None) -> Call:
        """The call to make. `last_problem` is why the last reply could not be used."""
        ...

    def problem_with(self, reply: Any) -> str | None:
        """Why a reply that arrived cannot be used, or None if it can."""
        ...

    async def retain(self, assignment: Mapping[str, Any], call: Call) -> str | None:
        """Make durable whatever the call left outside its reply, before the reply is
        recorded as usable, and name it: for a build, the commit its workspace was kept
        as. None for work whose reply is all there is."""
        ...


class Provider(Protocol):
    async def invoke(
        self, request: Request, *, on_event: Callable[[Event], None] | None = None
    ) -> ProviderResult: ...


class Worker:
    def __init__(
        self,
        session: WorkerSession,
        claims: Assignments,
        invocations: Invocations,
        provider: Provider,
        tasks: Mapping[str, Task],
    ) -> None:
        self._session = session
        self._claims = claims
        self._invocations = invocations
        self._provider = provider
        self._tasks = tasks
        self._recording: set[asyncio.Task[str]] = set()

    async def carry_out(self, assignment: Mapping[str, Any]) -> str:
        """Do what can be done for this work now, and say where that left it: `applied`,
        or the state its last call ended in, or `refused: ...` if nothing could start."""
        assignment_id: uuid.UUID = assignment["assignment_id"]
        task = self._tasks[assignment["kind"]]
        try:
            grant = await self._claims.claim(
                self._session.principal, assignment_id, self._session.worker_id
            )
        except ActionRefused as refused:
            return f"refused: {refused}"
        problem: str | None = None
        stale = 0
        while True:
            call = await task.prepare(assignment, problem)
            admission = await self._invocations.admit(
                self._session, assignment_id, grant, call.prepared
            )
            if admission.outcome == "stale" and stale < _PREPARATIONS:
                # What the briefing was written from moved while it was being written.
                stale += 1
                continue
            if admission.outcome in ("refused", "stale"):
                return f"refused: {admission.reason}"
            if admission.outcome == "finished":
                return "finished"
            assert admission.invocation_id is not None
            if admission.outcome == "apply":
                # A result is already known for this work. Nothing is called.
                return await self._invocations.apply(admission.invocation_id, self._session)
            result = await self._call(admission.invocation_id, task, assignment, call)
            state = await self._record(admission.invocation_id, task, assignment, call, result)
            if state == "returned":
                state = await self._invocations.apply(admission.invocation_id, self._session)
                if state != "rejected":
                    return state
                problem = await self._invocations.reason_of(admission.invocation_id)
            elif state in ("errored", "rejected"):
                problem = await self._invocations.reason_of(admission.invocation_id)
            else:
                return state
            # The call gave nothing usable. Whether another is allowed is admission's to
            # say, and the next briefing says what was wrong with this one.

    async def _record(
        self,
        invocation_id: uuid.UUID,
        task: Task,
        assignment: Mapping[str, Any],
        call: Call,
        result: ProviderResult,
    ) -> str:
        problem = _problem(task, result)
        retained = (
            await task.retain(assignment, call)
            if result.outcome is Outcome.RETURNED and problem is None and not result.unusable
            else None
        )
        return await self._invocations.record(
            invocation_id, result, problem=problem, retained=retained
        )

    async def _call(
        self, invocation_id: uuid.UUID, task: Task, assignment: Mapping[str, Any], call: Call
    ) -> ProviderResult:
        """Call the provider, and whatever happens, leave on record what is known of
        the call at the moment it is known: that it never started, that it ended with
        this result, or that nobody can say."""
        ended: list[Terminal] = []

        def listen(event: Event) -> None:
            if isinstance(event, Terminal):
                ended.append(event)

        try:
            return await self._provider.invoke(
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
                on_event=listen,
            )
        except ProviderUnavailable as unavailable:
            # Established before anything was launched: nothing was spent.
            return ProviderResult(Outcome.NOT_STARTED, reason=str(unavailable))
        except BaseException as cut_short:
            # Cancelled, or failed in a way nobody planned for, after the call may have
            # been launched. If its terminal event had been read, that is its result, and
            # it is recorded as any result is. Recorded by a task of its own: a caller
            # cancelled again must not take the record with it.
            known = (
                ProviderResult(
                    Outcome.RETURNED if ended[-1].succeeded else Outcome.ERRORED, ended[-1]
                )
                if ended
                else ProviderResult(
                    Outcome.UNCERTAIN, reason=f"the call was cut short: {cut_short!r}"
                )
            )
            recording = asyncio.create_task(
                self._record(invocation_id, task, assignment, call, known)
            )
            self._recording.add(recording)
            recording.add_done_callback(self._recording.discard)
            await asyncio.shield(recording)
            raise


def _problem(task: Task, result: ProviderResult) -> str | None:
    if result.outcome is not Outcome.RETURNED or result.terminal is None:
        return None
    reply = result.terminal.structured_output
    if reply is None:
        return "the reply did not carry the structured answer it was asked for"
    return task.problem_with(reply)
