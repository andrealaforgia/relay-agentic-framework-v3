"""Use cases that move a behaviour through its lifecycle. Each one commits the new state,
its ledger event and any outgoing command in a single transaction."""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from relay.domain.behaviour import Behaviour, BehaviourState
from relay.domain.evidence import (
    Execution,
    ProcessContract,
    RedVerdict,
    is_gate_bearing,
    refuse_isolation,
)
from relay.git.checkout import CheckoutFailed, pristine_checkout, trunk_head
from relay.persistence.database import Connection, Pool
from relay.persistence.ledger import append_event
from relay.persistence.outbox import enqueue
from relay.testing.oracle import ProcessOracle
from relay.transport.envelope import Envelope, MessageKind

CONTROL_PLANE = "relay/control-plane"


class Behaviours:
    def __init__(self, pool: Pool, oracle: ProcessOracle) -> None:
        self._pool = pool
        self._oracle = oracle

    async def open_project(self, project_id: str, repository: str, trunk: str = "main") -> None:
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute(
                "INSERT INTO projects (project_id, repository, trunk) VALUES (%s, %s, %s)",
                (project_id, repository, trunk),
            )
            await append_event(connection, project_id, "project.opened", payload={"trunk": trunk})

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

    async def run_red(self, project_id: str, behaviour_id: str) -> RedVerdict:
        """Observe the registered contract on the project's own trunk and judge the result.

        This is the only way a behaviour reaches the Builder. The caller chooses neither the
        source nor the observation: Relay reads the trunk head itself, launches the subject
        itself, and records the run, the verdict and any release in one transaction.
        """
        async with self._changing(project_id, behaviour_id) as (connection, behaviour, correlation):
            contract = behaviour.require_awaiting_red()
            isolation = self._oracle.isolation
            if not is_gate_bearing(isolation.name):
                refusal = refuse_isolation(contract, isolation.name)
                await self._red_rejected(
                    connection, project_id, behaviour_id, correlation, refusal, contract
                )
                return refusal
            repository, trunk = await _source(connection, project_id)

        try:
            revision = await trunk_head(repository, trunk)
            with tempfile.TemporaryDirectory(prefix="relay-red-") as scratch:
                workdir = await pristine_checkout(repository, revision, Path(scratch) / "subject")
                observation = await self._oracle.observe(workdir, contract.launch)
        except CheckoutFailed as failure:
            await self._red_preparation_failed(project_id, behaviour_id, contract, failure)
            raise
        execution = Execution(revision, isolation.name, isolation.runtime_identity, observation)

        async with self._changing(project_id, behaviour_id) as (connection, behaviour, correlation):
            verdict = behaviour.record_red_run(execution)
            execution_id = await _record_execution(
                connection, project_id, behaviour_id, "red", contract.digest, execution
            )
            evidence = {
                "execution_id": str(execution_id),
                "revision": execution.revision,
                "isolation": execution.isolation,
                "runtime_identity": execution.runtime_identity,
            }
            if not verdict.is_valid:
                await self._red_rejected(
                    connection, project_id, behaviour_id, correlation, verdict, contract, evidence
                )
                return verdict
            verified = await append_event(
                connection,
                project_id,
                "behaviour.test_red_verified",
                subject_id=behaviour_id,
                payload={
                    "reason": verdict.reason,
                    "contract_digest": contract.digest,
                    **evidence,
                },
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
                    idempotency_key=f"build:{project_id}:{behaviour_id}:{contract.digest}",
                    correlation_id=correlation,
                    causation_id=verified.event_id,
                    payload={"base_revision": execution.revision},
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

    async def _red_rejected(
        self,
        connection: Connection,
        project_id: str,
        behaviour_id: str,
        correlation: uuid.UUID,
        verdict: RedVerdict,
        contract: ProcessContract,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        assert verdict.rejection is not None
        await append_event(
            connection,
            project_id,
            "behaviour.red_rejected",
            subject_id=behaviour_id,
            payload={
                "rejection": verdict.rejection.value,
                "reason": verdict.reason,
                "contract_digest": contract.digest,
                **(evidence or {}),
            },
            correlation_id=correlation,
        )

    async def _red_preparation_failed(
        self, project_id: str, behaviour_id: str, contract: ProcessContract, failure: Exception
    ) -> None:
        """Nothing ran, and the ledger says so: a failed checkout is not a silent non-event."""
        async with self._changing(project_id, behaviour_id) as (connection, _, correlation):
            await append_event(
                connection,
                project_id,
                "behaviour.red_preparation_failed",
                subject_id=behaviour_id,
                payload={"reason": str(failure)[:500], "contract_digest": contract.digest},
                correlation_id=correlation,
            )

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


async def _source(connection: Connection, project_id: str) -> tuple[str, str]:
    row = await (
        await connection.execute(
            "SELECT repository, trunk FROM projects WHERE project_id = %s", (project_id,)
        )
    ).fetchone()
    assert row is not None
    return row["repository"], row["trunk"]


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
        "contract_digest, isolation, runtime_identity, exit_code, stdout, stderr, ineligibility) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            execution_id,
            project_id,
            behaviour_id,
            purpose,
            execution.revision,
            contract_digest,
            execution.isolation,
            execution.runtime_identity,
            observation.exit_code,
            observation.stdout,
            observation.stderr,
            observation.ineligibility.value if observation.ineligibility else None,
        ),
    )
    return execution_id
