"""Use cases that move a behaviour through its lifecycle. Each one commits the new state,
its ledger event and any outgoing command in a single transaction."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict

from psycopg.types.json import Jsonb

from relay.domain.behaviour import Behaviour, BehaviourState
from relay.domain.evidence import RedExpectation, RedVerdict, TestObservation
from relay.persistence.database import Connection, Pool
from relay.persistence.ledger import append_event
from relay.persistence.outbox import enqueue
from relay.transport.envelope import Envelope, MessageKind

CONTROL_PLANE = "relay/control-plane"


class Behaviours:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def open_project(self, project_id: str) -> None:
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute("INSERT INTO projects (project_id) VALUES (%s)", (project_id,))
            await append_event(connection, project_id, "project.opened")

    async def propose(self, project_id: str, behaviour_id: str, summary: str) -> None:
        behaviour = Behaviour.propose(behaviour_id, summary)
        correlation_id = uuid.uuid7()
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute(
                "INSERT INTO behaviours (project_id, behaviour_id, summary, state, correlation_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                (project_id, behaviour_id, summary, behaviour.state.value, correlation_id),
            )
            await append_event(
                connection,
                project_id,
                "behaviour.proposed",
                subject_id=behaviour_id,
                payload={"summary": summary},
                correlation_id=correlation_id,
            )

    async def begin_test_design(self, project_id: str, behaviour_id: str) -> None:
        async with self._changing(project_id, behaviour_id) as (connection, behaviour, correlation):
            behaviour.begin_test_design()
            await append_event(
                connection,
                project_id,
                "behaviour.test_design_started",
                subject_id=behaviour_id,
                correlation_id=correlation,
            )

    async def register_acceptance_test(
        self,
        project_id: str,
        behaviour_id: str,
        expectation: RedExpectation,
        contract_digest: str,
    ) -> None:
        async with self._changing(project_id, behaviour_id) as (connection, behaviour, correlation):
            behaviour.register_acceptance_test(expectation, contract_digest)
            await append_event(
                connection,
                project_id,
                "behaviour.acceptance_test_registered",
                subject_id=behaviour_id,
                payload={"expectation": asdict(expectation), "contract_digest": contract_digest},
                correlation_id=correlation,
            )

    async def record_red_run(
        self, project_id: str, behaviour_id: str, observations: Sequence[TestObservation]
    ) -> RedVerdict:
        async with self._changing(project_id, behaviour_id) as (connection, behaviour, correlation):
            verdict = behaviour.record_red_run(observations)
            if not verdict.is_valid:
                assert verdict.rejection is not None
                await append_event(
                    connection,
                    project_id,
                    "behaviour.red_rejected",
                    subject_id=behaviour_id,
                    payload={"rejection": verdict.rejection.value, "reason": verdict.reason},
                    correlation_id=correlation,
                )
                return verdict
            verified = await append_event(
                connection,
                project_id,
                "behaviour.test_red_verified",
                subject_id=behaviour_id,
                payload={"reason": verdict.reason, "contract_digest": behaviour.contract_digest},
                correlation_id=correlation,
            )
            await enqueue(
                connection,
                f"relay:{project_id}:cmd:builder",
                Envelope(
                    message_kind=MessageKind.COMMAND,
                    event_type="build.requested",
                    project_id=project_id,
                    behaviour_id=behaviour_id,
                    sender=CONTROL_PLANE,
                    recipient="builder",
                    idempotency_key=f"build:{project_id}:{behaviour_id}:{behaviour.contract_digest}",
                    correlation_id=correlation,
                    causation_id=verified.event_id,
                ),
            )
            return verdict

    async def state_of(self, project_id: str, behaviour_id: str) -> BehaviourState:
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT state FROM behaviours WHERE project_id = %s AND behaviour_id = %s",
                    (project_id, behaviour_id),
                )
            ).fetchone()
        if row is None:
            raise LookupError(f"unknown behaviour {behaviour_id!r} in project {project_id!r}")
        return BehaviourState(row["state"])

    @asynccontextmanager
    async def _changing(
        self, project_id: str, behaviour_id: str
    ) -> AsyncIterator[tuple[Connection, Behaviour, uuid.UUID]]:
        """Lock the behaviour, let the caller change it, and save it in the same transaction."""
        async with self._pool.connection() as connection, connection.transaction():
            row = await (
                await connection.execute(
                    "SELECT * FROM behaviours WHERE project_id = %s AND behaviour_id = %s "
                    "FOR UPDATE",
                    (project_id, behaviour_id),
                )
            ).fetchone()
            if row is None:
                raise LookupError(f"unknown behaviour {behaviour_id!r} in project {project_id!r}")
            expectation = row["red_expectation"]
            behaviour = Behaviour(
                behaviour_id=row["behaviour_id"],
                summary=row["summary"],
                state=BehaviourState(row["state"]),
                red_expectation=RedExpectation(**expectation) if expectation else None,
                contract_digest=row["contract_digest"],
            )
            yield connection, behaviour, row["correlation_id"]
            await connection.execute(
                "UPDATE behaviours SET state = %s, red_expectation = %s, contract_digest = %s "
                "WHERE project_id = %s AND behaviour_id = %s",
                (
                    behaviour.state.value,
                    Jsonb(asdict(behaviour.red_expectation)) if behaviour.red_expectation else None,
                    behaviour.contract_digest,
                    project_id,
                    behaviour_id,
                ),
            )
