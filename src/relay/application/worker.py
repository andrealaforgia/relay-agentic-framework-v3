"""What a worker does with one piece of work: claim it, and then, as often as it is
allowed, call the provider, record what came back, and apply it.

The worker is trusted code. The model is not: it is handed a briefing, it replies, and
everything that happens to Relay's state happens here, from the reply. The model is
given no way to call Relay.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from relay.application.assignments import Assignments
from relay.application.invocations import Invocations, Prepared
from relay.application.workers import WorkerSession
from relay.domain.story import ActionRefused
from relay.providers.claude import Confinement, Outcome, ProviderResult, Request


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


class Provider(Protocol):
    async def invoke(self, request: Request) -> ProviderResult: ...


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
        while True:
            call = await task.prepare(assignment, problem)
            admission = await self._invocations.admit(
                self._session, assignment_id, grant, call.prepared
            )
            if admission.outcome == "refused":
                return f"refused: {admission.reason}"
            if admission.outcome == "finished":
                return "finished"
            assert admission.invocation_id is not None
            if admission.outcome == "apply":
                # A result is already known for this work. Nothing is called.
                return await self._invocations.apply(admission.invocation_id, self._session)
            result = await self._provider.invoke(
                Request(
                    invocation_id=admission.invocation_id,
                    definition=call.prepared.definition,
                    model=call.prepared.model,
                    effort=call.prepared.effort,
                    briefing=call.prepared.briefing,
                    schema=call.prepared.schema,
                    workdir=call.workdir,
                    confinement=call.confinement,
                    timeout=call.timeout,
                    budget_usd=call.budget_usd,
                )
            )
            problem = _problem(task, result)
            state = await self._invocations.record(admission.invocation_id, result, problem=problem)
            if state == "returned":
                state = await self._invocations.apply(admission.invocation_id, self._session)
                if state != "rejected":
                    return state
                problem = await self._invocations.reason_of(admission.invocation_id)
            elif state == "errored":
                problem = await self._invocations.reason_of(admission.invocation_id)
            elif state != "rejected":
                return state
            # The call gave nothing usable. Whether another is allowed is admission's to
            # say, and the next briefing says what was wrong with this one.


def _problem(task: Task, result: ProviderResult) -> str | None:
    if result.outcome is not Outcome.RETURNED or result.terminal is None:
        return None
    reply = result.terminal.structured_output
    if reply is None:
        return "the reply did not carry the structured answer it was asked for"
    return task.problem_with(reply)
