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

from relay.application.reviews import open_cycle
from relay.domain.behaviour import Behaviour, BehaviourState
from relay.domain.evidence import (
    Execution,
    GreenVerdict,
    PairedRed,
    ProcessContract,
    RedVerdict,
    is_gate_bearing,
    refuse_green_before_launch,
    refuse_isolation,
)
from relay.domain.identifiers import require_identifier
from relay.git.checkout import (
    CheckoutFailed,
    attempt_head,
    attempt_ref,
    pristine_checkout,
    require_credential_free,
    trunk_head,
)
from relay.git.mirror import Mirror
from relay.persistence.database import Connection, Pool
from relay.persistence.ledger import append_event
from relay.persistence.outbox import enqueue
from relay.testing.oracle import ProcessOracle
from relay.transport.envelope import Envelope, MessageKind

CONTROL_PLANE = "relay/control-plane"


class Behaviours:
    def __init__(self, pool: Pool, mirrors: Path, oracle: ProcessOracle) -> None:
        """`mirrors` is a directory on the control plane's own disk where every commit
        Relay judges is kept pinned, one bare repository per project."""
        self._pool = pool
        self._mirrors = mirrors
        self._oracle = oracle

    def _mirror(self, project_id: str) -> Mirror:
        return Mirror(self._mirrors / f"{require_identifier('project', project_id)}.git")

    async def open_project(self, project_id: str, repository: str, trunk: str = "main") -> None:
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute(
                "INSERT INTO projects (project_id, repository, trunk) VALUES (%s, %s, %s)",
                (
                    require_identifier("project", project_id),
                    require_credential_free(repository),
                    trunk,
                ),
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
            cycle = behaviour.red_cycle
            isolation = self._oracle.isolation
            if not is_gate_bearing(isolation.name):
                refusal = refuse_isolation(contract, isolation.name)
                await self._red_rejected(
                    connection, project_id, behaviour_id, correlation, refusal, contract
                )
                return refusal
            repository, trunk = await source_of(connection, project_id)

        try:
            revision = await trunk_head(repository, trunk)
            with tempfile.TemporaryDirectory(prefix="relay-red-") as scratch:
                workdir = await pristine_checkout(repository, revision, Path(scratch) / "subject")
                execution = await self._oracle.execute(revision, workdir, contract.launch)
        except CheckoutFailed as failure:
            await self._preparation_failed("red", project_id, behaviour_id, contract, failure)
            raise

        async with self._changing(project_id, behaviour_id) as (connection, behaviour, correlation):
            verdict = behaviour.record_red_run(execution, started_in_cycle=cycle)
            execution_id = await record_execution(
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
            await connection.execute(
                "UPDATE behaviours SET red_execution_id = %s "
                "WHERE project_id = %s AND behaviour_id = %s",
                (execution_id, project_id, behaviour_id),
            )
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
                    # One build request per accepted red: a redelivery keeps the key, a
                    # fresh red after reopening gets a new one.
                    idempotency_key=f"build:{project_id}:{behaviour_id}:{execution_id}",
                    correlation_id=correlation,
                    causation_id=verified.event_id,
                    payload={"base_revision": execution.revision},
                ),
            )
            return verdict

    async def run_green(self, project_id: str, behaviour_id: str, attempt: int) -> GreenVerdict:
        """Observe the contract on the candidate a Builder published for this attempt.

        The caller names an attempt, nothing more. Relay reads the candidate from the
        project's own repository, checks it was built on the revision the red was observed
        on, launches it itself and pairs the result with that red.
        """
        async with self._changing(project_id, behaviour_id) as (connection, behaviour, _):
            contract = behaviour.require_awaiting_green()
            isolation = self._oracle.isolation
            repository, _ = await source_of(connection, project_id)
            red = await _paired_red(connection, project_id, behaviour_id)

        if refusal := refuse_green_before_launch(contract, isolation.name, red):
            await self._green_refused(project_id, behaviour_id, attempt, contract, refusal)
            return refusal
        assert red is not None
        try:
            candidate = await attempt_head(repository, project_id, behaviour_id, attempt)
            # Pin the candidate before anything depends on it: the Builder may move the
            # attempt ref at any time, and this commit is about to become evidence.
            mirror = self._mirror(project_id)
            await mirror.pin_from(
                repository, attempt_ref(project_id, behaviour_id, attempt), candidate
            )
            descends = await mirror.descends(candidate, red.revision)
            with tempfile.TemporaryDirectory(prefix="relay-green-") as scratch:
                workdir = await mirror.export(candidate, Path(scratch) / "subject")
                execution = await self._oracle.execute(candidate, workdir, contract.launch)
        except CheckoutFailed as failure:
            await self._preparation_failed("green", project_id, behaviour_id, contract, failure)
            raise
        return await self._record_green(
            project_id, behaviour_id, attempt, execution, red, descends=descends
        )

    async def reopen_red(self, project_id: str, behaviour_id: str, reason: str) -> None:
        """Send a behaviour back for a fresh red and drop the old pairing, on the record."""
        if not reason.strip():
            raise ValueError("reopening a red needs a stated reason")
        async with self._changing(project_id, behaviour_id) as (connection, behaviour, correlation):
            behaviour.reopen_red()
            await connection.execute(
                "UPDATE behaviours SET red_execution_id = NULL "
                "WHERE project_id = %s AND behaviour_id = %s",
                (project_id, behaviour_id),
            )
            await append_event(
                connection,
                project_id,
                "behaviour.red_reopened",
                subject_id=behaviour_id,
                payload={"reason": reason},
                correlation_id=correlation,
            )

    async def _green_refused(
        self,
        project_id: str,
        behaviour_id: str,
        attempt: int,
        contract: ProcessContract,
        refusal: GreenVerdict,
    ) -> None:
        """Nothing was launched and no execution is claimed; the refusal is still recorded."""
        assert refusal.rejection is not None
        async with self._changing(project_id, behaviour_id) as (connection, _, correlation):
            await append_event(
                connection,
                project_id,
                "behaviour.green_rejected",
                subject_id=behaviour_id,
                payload={
                    "attempt": attempt,
                    "rejection": refusal.rejection.value,
                    "reason": refusal.reason,
                    "contract_digest": contract.digest,
                },
                correlation_id=correlation,
            )

    async def _record_green(
        self,
        project_id: str,
        behaviour_id: str,
        attempt: int,
        execution: Execution,
        red: PairedRed,
        *,
        descends: bool,
    ) -> GreenVerdict:
        async with self._changing(project_id, behaviour_id) as (connection, behaviour, correlation):
            verdict = behaviour.record_green_run(
                execution,
                red,
                await _paired_red(connection, project_id, behaviour_id),
                candidate_descends_from_red=descends,
            )
            contract = behaviour.contract
            assert contract is not None
            execution_id = await record_execution(
                connection, project_id, behaviour_id, "green", contract.digest, execution
            )
            evidence: dict[str, Any] = {
                "attempt": attempt,
                "reason": verdict.reason,
                "contract_digest": contract.digest,
                "red_revision": red.revision,
                "execution_id": str(execution_id),
                "candidate_revision": execution.revision,
                "isolation": execution.isolation,
                "runtime_identity": execution.runtime_identity,
            }
            if verdict.rejection is not None:
                evidence["rejection"] = verdict.rejection.value
            else:
                await connection.execute(
                    "UPDATE behaviours SET candidate_revision = %s "
                    "WHERE project_id = %s AND behaviour_id = %s",
                    (execution.revision, project_id, behaviour_id),
                )
                cycle_id = await open_cycle(
                    connection,
                    project_id,
                    behaviour_id,
                    green_execution_id=execution_id,
                    red_execution_id=red.execution_id,
                    candidate=execution.revision,
                )
                evidence["review_cycle_id"] = str(cycle_id)
            await append_event(
                connection,
                project_id,
                "behaviour.green_verified" if verdict.is_valid else "behaviour.green_rejected",
                subject_id=behaviour_id,
                payload=evidence,
                correlation_id=correlation,
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

    async def _preparation_failed(
        self,
        colour: str,
        project_id: str,
        behaviour_id: str,
        contract: ProcessContract,
        failure: Exception,
    ) -> None:
        """Nothing ran, and the ledger says so: a failed checkout is not a silent non-event."""
        async with self._changing(project_id, behaviour_id) as (connection, _, correlation):
            await append_event(
                connection,
                project_id,
                f"behaviour.{colour}_preparation_failed",
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
            behaviour = load_behaviour(row)
            yield connection, behaviour, row["correlation_id"]
            await save_behaviour(connection, project_id, behaviour)


def load_behaviour(row: dict[str, Any]) -> Behaviour:
    contract = row["contract"]
    return Behaviour(
        behaviour_id=row["behaviour_id"],
        summary=row["summary"],
        state=BehaviourState(row["state"]),
        contract=ProcessContract.from_dict(contract) if contract else None,
        red_cycle=row["red_cycle"],
        reopened_from_review=row["reopened_from_review"],
    )


async def save_behaviour(connection: Connection, project_id: str, behaviour: Behaviour) -> None:
    await connection.execute(
        "UPDATE behaviours SET state = %s, contract = %s, red_cycle = %s, "
        "reopened_from_review = %s WHERE project_id = %s AND behaviour_id = %s",
        (
            behaviour.state.value,
            Jsonb(behaviour.contract.to_dict()) if behaviour.contract else None,
            behaviour.red_cycle,
            behaviour.reopened_from_review,
            project_id,
            behaviour.behaviour_id,
        ),
    )


async def source_of(connection: Connection, project_id: str) -> tuple[str, str]:
    row = await (
        await connection.execute(
            "SELECT repository, trunk FROM projects WHERE project_id = %s", (project_id,)
        )
    ).fetchone()
    assert row is not None
    return row["repository"], row["trunk"]


async def _paired_red(
    connection: Connection, project_id: str, behaviour_id: str
) -> PairedRed | None:
    row = await (
        await connection.execute(
            "SELECT e.execution_id::text, e.revision, e.contract_digest, e.runtime_identity "
            "FROM behaviours b JOIN executions e ON e.execution_id = b.red_execution_id "
            "WHERE b.project_id = %s AND b.behaviour_id = %s",
            (project_id, behaviour_id),
        )
    ).fetchone()
    return PairedRed(**row) if row else None


async def record_execution(
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
