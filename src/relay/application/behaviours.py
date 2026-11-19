"""Use cases that move a behaviour through its lifecycle. Each one commits the new state,
its ledger event and any outgoing command in a single transaction."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg.types.json import Jsonb

from relay.domain.behaviour import Behaviour, BehaviourState
from relay.domain.evidence import EvidencePolicy, Execution, ProcessContract, RedVerdict
from relay.persistence.database import Connection, Pool
from relay.persistence.ledger import append_event
from relay.persistence.outbox import enqueue
from relay.transport.envelope import Envelope, MessageKind

CONTROL_PLANE = "relay/control-plane"


class Behaviours:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def open_project(
        self, project_id: str, *, native_evidence_approved: bool = False
    ) -> None:
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute(
                "INSERT INTO projects (project_id, native_evidence_approved) VALUES (%s, %s)",
                (project_id, native_evidence_approved),
            )
            await append_event(
                connection,
                project_id,
                "project.opened",
                payload={"native_evidence_approved": native_evidence_approved},
            )

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
        self, project_id: str, behaviour_id: str, contract: ProcessContract
    ) -> None:
        async with self._changing(project_id, behaviour_id) as (connection, behaviour, correlation):
            behaviour.register_acceptance_test(contract)
            await append_event(
                connection,
                project_id,
                "behaviour.acceptance_test_registered",
                subject_id=behaviour_id,
                payload={"contract": contract.to_dict(), "contract_digest": contract.digest},
                correlation_id=correlation,
            )

    async def record_red_run(
        self, project_id: str, behaviour_id: str, execution: Execution
    ) -> RedVerdict:
        """Judge one oracle run. The run, the verdict and any release commit together."""
        async with self._changing(project_id, behaviour_id) as (connection, behaviour, correlation):
            verdict = behaviour.record_red_run(execution, await _policy(connection, project_id))
            assert behaviour.contract is not None
            contract_digest = behaviour.contract.digest
            execution_id = await _record_execution(
                connection, project_id, behaviour_id, "red", contract_digest, execution
            )
            evidence = {
                "reason": verdict.reason,
                "execution_id": str(execution_id),
                "revision": execution.revision,
                "isolation": execution.isolation,
                "contract_digest": contract_digest,
            }
            if not verdict.is_valid:
                assert verdict.rejection is not None
                await append_event(
                    connection,
                    project_id,
                    "behaviour.red_rejected",
                    subject_id=behaviour_id,
                    payload={"rejection": verdict.rejection.value, **evidence},
                    correlation_id=correlation,
                )
                return verdict
            verified = await append_event(
                connection,
                project_id,
                "behaviour.test_red_verified",
                subject_id=behaviour_id,
                payload=evidence,
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
                    idempotency_key=f"build:{project_id}:{behaviour_id}:{contract_digest}",
                    correlation_id=correlation,
                    causation_id=verified.event_id,
                    payload={"base_revision": execution.revision},
                ),
            )
            return verdict

    async def contract_of(self, project_id: str, behaviour_id: str) -> ProcessContract:
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT contract FROM behaviours WHERE project_id = %s AND behaviour_id = %s",
                    (project_id, behaviour_id),
                )
            ).fetchone()
        if row is None or row["contract"] is None:
            raise LookupError(f"behaviour {behaviour_id!r} has no registered acceptance contract")
        return ProcessContract.from_dict(row["contract"])

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
            contract = row["contract"]
            behaviour = Behaviour(
                behaviour_id=row["behaviour_id"],
                summary=row["summary"],
                state=BehaviourState(row["state"]),
                contract=ProcessContract.from_dict(contract) if contract else None,
            )
            yield connection, behaviour, row["correlation_id"]
            await connection.execute(
                "UPDATE behaviours SET state = %s, contract = %s "
                "WHERE project_id = %s AND behaviour_id = %s",
                (
                    behaviour.state.value,
                    Jsonb(behaviour.contract.to_dict()) if behaviour.contract else None,
                    project_id,
                    behaviour_id,
                ),
            )


async def _policy(connection: Connection, project_id: str) -> EvidencePolicy:
    row = await (
        await connection.execute(
            "SELECT native_evidence_approved FROM projects WHERE project_id = %s", (project_id,)
        )
    ).fetchone()
    assert row is not None
    return EvidencePolicy(native_approved=row["native_evidence_approved"])


async def _record_execution(
    connection: Connection,
    project_id: str,
    behaviour_id: str,
    purpose: str,
    contract_digest: str,
    execution: Execution,
) -> uuid.UUID:
    execution_id = uuid.uuid7()
    observation = execution.observation
    await connection.execute(
        "INSERT INTO executions (execution_id, project_id, behaviour_id, purpose, revision, "
        "contract_digest, isolation, exit_code, stdout, stderr, ineligibility) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            execution_id,
            project_id,
            behaviour_id,
            purpose,
            execution.revision,
            contract_digest,
            execution.isolation,
            observation.exit_code,
            observation.stdout,
            observation.stderr,
            observation.ineligibility.value if observation.ineligibility else None,
        ),
    )
    return execution_id
