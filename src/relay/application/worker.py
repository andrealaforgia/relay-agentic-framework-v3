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
        self,
        request: Request,
        *,
        on_result: Callable[[ProviderResult], None] | None = None,
        on_start: Callable[[int], None] | None = None,
    ) -> ProviderResult:
        """`on_start` is called when the provider has launched the call, and not before;
        `on_result` when it knows how the call ended, as it classified it."""
        ...


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
            # The call gave nothing usable. Whether another is allowed is admission's to
            # say, and the next briefing says what was wrong with this one.

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
            problem = _problem(task, result)
        except Exception as fault:  # noqa: BLE001 - a fault in the check loses no result
            # The reply arrived and the check of it failed, which is not the same as the
            # reply failing the check. Either way the call and what it used are recorded.
            problem = f"the reply could not be checked: {fault!r}"
        retained = None
        if result.outcome is Outcome.RETURNED and problem is None and not result.unusable:
            try:
                retained = await task.retain(assignment, call)
            except Exception as failure:  # noqa: BLE001 - whatever stopped it is recorded
                # The reply arrived and what it left could not be kept. That is known,
                # and is recorded with what the call used.
                problem = f"what the call left could not be kept: {failure}"
        return await self._invocations.record(
            invocation_id, result, problem=problem, retained=retained
        )


def _problem(task: Task, result: ProviderResult) -> str | None:
    if result.outcome is not Outcome.RETURNED or result.terminal is None:
        return None
    reply = result.terminal.structured_output
    if reply is None:
        return "the reply did not carry the structured answer it was asked for"
    return task.problem_with(reply)
