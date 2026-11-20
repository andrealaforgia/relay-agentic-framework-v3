"""Integration: a reviewed candidate becomes trunk, or it does not, and either way the
record says which.

Three things are kept apart here. Checks run outside any lock and prove nothing until
their batch is accepted. Acceptance happens in one transaction that looks at everything
again and records an intent. Only the intent moves trunk, and from the moment it exists
it is settled by what the repository says, never by what a push call seemed to do.
"""

from __future__ import annotations

import tempfile
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from relay.application.behaviours import (
    CONTROL_PLANE,
    load_behaviour,
    record_execution,
    save_behaviour,
)
from relay.application.reviews import gate_of
from relay.domain.behaviour import Behaviour, BehaviourState
from relay.domain.evidence import CheckContract, Execution, ProcessContract, is_gate_bearing
from relay.domain.identifiers import require_identifier
from relay.domain.integration import (
    TrunkReading,
    a_rejection_settles_the_intent,
    read_trunk,
    regression_digest,
)
from relay.domain.review import GateState
from relay.git.checkout import CheckoutFailed, RemoteRejected, trunk_head
from relay.git.mirror import Mirror
from relay.persistence.database import Connection, Pool
from relay.persistence.ledger import append_event
from relay.persistence.outbox import enqueue
from relay.testing.oracle import ProcessOracle
from relay.transport.envelope import Envelope, MessageKind


class IntegrationRefused(Exception):
    """The request cannot be acted on as things stand. Nothing was run or changed."""


class Outcome(StrEnum):
    INTEGRATED = "integrated"
    CHECKS_FAILED = "checks_failed"
    BASE_MOVED = "base_moved"
    # The batch finished but may not decide anything: another request, a change of
    # policy or another integration got there first. Its runs are kept as evidence.
    SUPERSEDED = "superseded"
    IN_PROGRESS = "in_progress"
    BUSY = "busy"
    # Authorised, and trunk has not yet been seen to hold the candidate.
    PENDING = "pending"
    REJECTED = "rejected"
    CONFLICTED = "conflicted"


@dataclass(frozen=True)
class IntegrationReceipt:
    outcome: Outcome
    batch_id: uuid.UUID | None = None
    reason: str = ""


_OUTCOME_OF_BATCH = {
    "running": Outcome.IN_PROGRESS,
    "failed": Outcome.CHECKS_FAILED,
    "superseded": Outcome.SUPERSEDED,
    "discarded": Outcome.SUPERSEDED,
    "abandoned": Outcome.SUPERSEDED,
    "base_moved": Outcome.BASE_MOVED,
}
_OUTCOME_OF_INTENT = {
    "outstanding": Outcome.PENDING,
    "integrated": Outcome.INTEGRATED,
    "rejected": Outcome.REJECTED,
    "conflicted": Outcome.CONFLICTED,
}


class Integration:
    def __init__(
        self, pool: Pool, mirrors: Path, oracle: ProcessOracle, push_timeout: float = 120.0
    ) -> None:
        self._pool = pool
        self._mirrors = mirrors
        self._oracle = oracle
        self._push_timeout = push_timeout

    def _mirror(self, project_id: str) -> Mirror:
        return Mirror(self._mirrors / f"{require_identifier('project', project_id)}.git")

    async def require_check(self, project_id: str, check: CheckContract) -> None:
        """From now on every candidate of this project must pass this check before it can
        reach trunk. A batch already running was started under the old set and will not
        be allowed to decide anything."""
        async with self._pool.connection() as connection, connection.transaction():
            try:
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO required_checks (project_id, contract_id, contract, "
                        "contract_digest) VALUES (%s, %s, %s, %s)",
                        (project_id, check.contract_id, Jsonb(check.to_dict()), check.digest),
                    )
            except UniqueViolation:
                raise ValueError(
                    f"a check named {check.contract_id!r} is already required"
                ) from None
            await connection.execute(
                "UPDATE projects SET check_policy_version = check_policy_version + 1 "
                "WHERE project_id = %s",
                (project_id,),
            )
            await append_event(
                connection,
                project_id,
                "project.check_required",
                payload={"contract": check.to_dict(), "contract_digest": check.digest},
            )

    async def integrate(
        self, project_id: str, behaviour_id: str, request_id: str
    ) -> IntegrationReceipt:
        """Check the behaviour's reviewed candidate and, if everything holds, move trunk to
        it. A request repeated with the same id is answered from what the first one did;
        it never starts the checks again."""
        require_identifier("request", request_id)
        if not is_gate_bearing(self._oracle.isolation.name):
            raise IntegrationRefused(
                f"{self._oracle.isolation.name} execution cannot open a gate; "
                "checks must be observed in a container"
            )
        async with self._pool.connection() as connection:
            earlier = await _batch_of_request(connection, project_id, behaviour_id, request_id)
            source = await (
                await connection.execute(
                    "SELECT repository, trunk FROM projects WHERE project_id = %s", (project_id,)
                )
            ).fetchone()
        if earlier is not None:
            return await self._answer_again(earlier)
        if source is None:
            raise LookupError(f"unknown project {project_id!r}")
        head = await trunk_head(source["repository"], source["trunk"])
        started = await self._start_batch(project_id, behaviour_id, request_id, head)
        if isinstance(started, IntegrationReceipt):
            return started
        try:
            executions = await self._run_checks(started)
        except BaseException:
            await self._abandon(started["batch_id"])
            raise
        settled = await self._settle_batch(started, executions)
        if isinstance(settled, IntegrationReceipt):
            return settled
        return await self._drive(settled)

    async def reconcile(self) -> int:
        """Settle every authorised integration whose outcome is not yet on record, from
        what trunk holds now. Run on start and whenever a push went unanswered."""
        async with self._pool.connection() as connection:
            outstanding = await (
                await connection.execute(
                    "SELECT intent_id FROM integration_intents WHERE state = 'outstanding' "
                    "ORDER BY created_at"
                )
            ).fetchall()
        settled = 0
        for row in outstanding:
            try:
                receipt = await self._reconcile(row["intent_id"])
            except CheckoutFailed:
                continue  # nothing learned about this one; the others still get their turn
            settled += receipt.outcome is not Outcome.PENDING
        return settled

    async def retry_integration(self, project_id: str, behaviour_id: str, reason: str) -> None:
        """Put a behaviour whose integration the repository refused, or found trunk
        elsewhere, back where it can be integrated again. A person decides this."""
        if not reason.strip():
            raise ValueError("retrying an integration needs a stated reason")
        async with self._pool.connection() as connection, connection.transaction():
            behaviour, _ = await _locked_behaviour(connection, project_id, behaviour_id)
            last = await (
                await connection.execute(
                    "SELECT i.state, c.state AS cycle_state FROM integration_intents i "
                    "JOIN review_cycles c USING (cycle_id) WHERE i.project_id = %s "
                    "AND i.behaviour_id = %s ORDER BY i.created_at DESC LIMIT 1",
                    (project_id, behaviour_id),
                )
            ).fetchone()
            if (
                last is None
                or last["state"] not in ("rejected", "conflicted")
                or last["cycle_state"] != "satisfied"
            ):
                raise IntegrationRefused("this behaviour has no failed integration to retry")
            behaviour.retry_integration()
            await save_behaviour(connection, project_id, behaviour)
            await append_event(
                connection,
                project_id,
                "integration.retry_allowed",
                subject_id=behaviour_id,
                payload={"reason": reason},
            )

    async def _start_batch(
        self, project_id: str, behaviour_id: str, request_id: str, head: str
    ) -> dict[str, Any] | IntegrationReceipt:
        answer_again: dict[str, Any] | None = None
        reconcile: uuid.UUID | None = None
        async with self._pool.connection() as connection, connection.transaction():
            behaviour, row = await _locked_behaviour(connection, project_id, behaviour_id)
            answer_again = await _batch_of_request(connection, project_id, behaviour_id, request_id)
            if answer_again is None:
                cycle = await _live_cycle(connection, project_id, behaviour_id)
                if (
                    behaviour.state is not BehaviourState.REVIEW
                    or cycle is None
                    or cycle["state"] != "satisfied"
                ):
                    raise IntegrationRefused(
                        f"{behaviour_id} has no candidate whose review gate is satisfied"
                    )
                project = await _locked_project(connection, project_id)
                intent = await _outstanding_intent(connection, project_id)
                if intent is not None and intent["behaviour_id"] != behaviour_id:
                    return IntegrationReceipt(
                        Outcome.BUSY,
                        reason=f"the integration of {intent['behaviour_id']} is not settled yet",
                    )
                if intent is not None:
                    reconcile = intent["intent_id"]
                else:
                    checks = await _regression_set(connection, project_id)
                    batch = {
                        "batch_id": uuid.uuid7(),
                        "project_id": project_id,
                        "behaviour_id": behaviour_id,
                        "request_id": request_id,
                        "cycle_id": cycle["cycle_id"],
                        "candidate_revision": cycle["candidate_revision"],
                        "expected_trunk": head,
                        "check_policy_version": project["check_policy_version"],
                        "regression_digest": regression_digest(
                            project["check_policy_version"], checks
                        ),
                        "members": checks,
                    }
                    if head != cycle["red_revision"]:
                        await _insert_batch(connection, batch, state="base_moved")
                        await self._reopen(
                            connection,
                            behaviour,
                            cycle,
                            "trunk is no longer the revision the red was observed on",
                            {"red_revision": cycle["red_revision"], "trunk": head},
                        )
                        return IntegrationReceipt(Outcome.BASE_MOVED, batch["batch_id"])
                    await connection.execute(
                        "UPDATE check_batches SET state = 'superseded', "
                        "reason = 'a later request started the checks again' "
                        "WHERE cycle_id = %s AND state = 'running'",
                        (cycle["cycle_id"],),
                    )
                    await _insert_batch(connection, batch, state="running")
                    await append_event(
                        connection,
                        project_id,
                        "integration.checks_started",
                        subject_id=behaviour_id,
                        payload={
                            "batch_id": str(batch["batch_id"]),
                            "cycle_id": str(cycle["cycle_id"]),
                            "candidate_revision": cycle["candidate_revision"],
                            "expected_trunk": head,
                            "checks": sorted(check.contract_id for check in checks),
                        },
                        correlation_id=row["correlation_id"],
                    )
                    return batch
        if reconcile is not None:
            return await self._reconcile(reconcile)
        assert answer_again is not None
        return await self._answer_again(answer_again)

    async def _reopen(
        self,
        connection: Connection,
        behaviour: Behaviour,
        cycle: dict[str, Any],
        reason: str,
        details: dict[str, Any],
    ) -> None:
        project_id, behaviour_id = cycle["project_id"], cycle["behaviour_id"]
        behaviour.reopen_from_review()
        await save_behaviour(connection, project_id, behaviour)
        await connection.execute(
            "UPDATE behaviours SET red_execution_id = NULL "
            "WHERE project_id = %s AND behaviour_id = %s",
            (project_id, behaviour_id),
        )
        await connection.execute(
            "UPDATE review_cycles SET state = 'superseded' WHERE cycle_id = %s",
            (cycle["cycle_id"],),
        )
        await append_event(
            connection,
            project_id,
            "integration.red_reopened",
            subject_id=behaviour_id,
            payload={"cycle_id": str(cycle["cycle_id"]), "reason": reason, **details},
        )

    async def _run_checks(self, batch: dict[str, Any]) -> list[tuple[CheckContract, Execution]]:
        mirror = self._mirror(batch["project_id"])
        candidate = batch["candidate_revision"]
        executions = []
        for check in batch["members"]:
            # A tree of its own for every check: what one run leaves behind must not be
            # what the next one observes.
            with tempfile.TemporaryDirectory(prefix="relay-check-") as scratch:
                workdir = await mirror.export(candidate, Path(scratch) / "subject")
                execution = await self._oracle.execute(candidate, workdir, check.launch)
            executions.append((check, execution))
        return executions

    async def _abandon(self, batch_id: uuid.UUID) -> None:
        async with self._pool.connection() as connection:
            await connection.execute(
                "UPDATE check_batches SET state = 'abandoned', "
                "reason = 'the checks could not be completed' "
                "WHERE batch_id = %s AND state = 'running'",
                (batch_id,),
            )

    async def _settle_batch(
        self, batch: dict[str, Any], executions: list[tuple[CheckContract, Execution]]
    ) -> uuid.UUID | IntegrationReceipt:
        """Decide what a finished batch means. Its runs are recorded whatever happens; it
        may only send work back or authorise an integration if nothing it relied on has
        changed since it started."""
        project_id, behaviour_id = batch["project_id"], batch["behaviour_id"]
        batch_id, cycle_id = batch["batch_id"], batch["cycle_id"]
        async with self._pool.connection() as connection, connection.transaction():
            behaviour, row = await _locked_behaviour(connection, project_id, behaviour_id)
            project = await _locked_project(connection, project_id)
            runs = []
            for check, execution in executions:
                execution_id = await record_execution(
                    connection, project_id, behaviour_id, "check", check.digest, execution
                )
                satisfied = check.is_satisfied_by(execution)
                await connection.execute(
                    "INSERT INTO check_runs (batch_id, contract_digest, contract_id, "
                    "execution_id, satisfied) VALUES (%s, %s, %s, %s, %s)",
                    (batch_id, check.digest, check.contract_id, execution_id, satisfied),
                )
                runs.append((check, execution, execution_id, satisfied))
            cycle = await _live_cycle(connection, project_id, behaviour_id)
            current = await (
                await connection.execute(
                    "SELECT state FROM check_batches WHERE batch_id = %s FOR UPDATE", (batch_id,)
                )
            ).fetchone()
            assert current is not None
            if current["state"] != "running":
                return IntegrationReceipt(Outcome.SUPERSEDED, batch_id)
            stale = await _why_stale(connection, batch, behaviour, row, cycle, project)
            if stale:
                await _close_batch(connection, batch, "discarded", stale)
                return IntegrationReceipt(Outcome.SUPERSEDED, batch_id, stale)
            assert cycle is not None
            if any(
                execution.observation.is_eligible
                and execution.runtime_identity != cycle["green_runtime"]
                for _, execution, _, _ in runs
            ):
                reason = "the runtime is no longer the one the green was observed in"
                await _close_batch(connection, batch, "discarded", reason)
                await self._reopen(connection, behaviour, cycle, reason, {})
                return IntegrationReceipt(Outcome.BASE_MOVED, batch_id, reason)
            failed = [
                {"contract_id": check.contract_id, "execution_id": str(execution_id)}
                for check, _, execution_id, satisfied in runs
                if not satisfied
            ]
            if failed:
                await self._send_back(connection, batch, behaviour, row, failed)
                return IntegrationReceipt(Outcome.CHECKS_FAILED, batch_id)
            intent_id = uuid.uuid7()
            await connection.execute(
                "INSERT INTO integration_intents (intent_id, project_id, behaviour_id, cycle_id, "
                "batch_id, candidate_revision, expected_trunk) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    intent_id,
                    project_id,
                    behaviour_id,
                    cycle_id,
                    batch_id,
                    batch["candidate_revision"],
                    batch["expected_trunk"],
                ),
            )
            await _close_batch(connection, batch, "authorised", None, event=False)
            submissions = await (
                await connection.execute(
                    "SELECT s.submission_id::text FROM review_submissions s "
                    "JOIN review_tasks t USING (task_id) WHERE t.cycle_id = %s "
                    "AND s.disposition = 'effective' ORDER BY 1",
                    (cycle_id,),
                )
            ).fetchall()
            await append_event(
                connection,
                project_id,
                "integration.authorised",
                subject_id=behaviour_id,
                payload={
                    "intent_id": str(intent_id),
                    "batch_id": str(batch_id),
                    "cycle_id": str(cycle_id),
                    "candidate_revision": batch["candidate_revision"],
                    "expected_trunk": batch["expected_trunk"],
                    "check_policy_version": batch["check_policy_version"],
                    "green_execution_id": str(cycle["green_execution_id"]),
                    "check_execution_ids": [str(execution_id) for _, _, execution_id, _ in runs],
                    "review_submission_ids": [s["submission_id"] for s in submissions],
                },
                correlation_id=row["correlation_id"],
            )
            return intent_id

    async def _send_back(
        self,
        connection: Connection,
        batch: dict[str, Any],
        behaviour: Behaviour,
        row: dict[str, Any],
        failed: list[dict[str, str]],
    ) -> None:
        project_id, behaviour_id, cycle_id = (
            batch["project_id"],
            batch["behaviour_id"],
            batch["cycle_id"],
        )
        behaviour.request_changes()
        await save_behaviour(connection, project_id, behaviour)
        await connection.execute(
            "UPDATE review_cycles SET state = 'changes_requested' WHERE cycle_id = %s",
            (cycle_id,),
        )
        await connection.execute(
            "INSERT INTO remediation_requests (cycle_id, finding_ids, failed_checks) "
            "VALUES (%s, '{}', %s)",
            (cycle_id, Jsonb(failed)),
        )
        await _close_batch(connection, batch, "failed", None, event=False)
        decided = await append_event(
            connection,
            project_id,
            "integration.checks_failed",
            subject_id=behaviour_id,
            payload={
                "batch_id": str(batch["batch_id"]),
                "cycle_id": str(cycle_id),
                "failed_checks": failed,
            },
            correlation_id=row["correlation_id"],
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
                idempotency_key=f"remediate:{cycle_id}",
                correlation_id=row["correlation_id"],
                causation_id=decided.event_id,
                payload={
                    "base_revision": batch["candidate_revision"],
                    "failed_checks": [check["contract_id"] for check in failed],
                },
            ),
        )

    async def _drive(self, intent_id: uuid.UUID) -> IntegrationReceipt:
        """Send the authorised update. The send is counted before it leaves: if this
        process dies now, the record still says a push may be on its way."""
        async with self._pool.connection() as connection, connection.transaction():
            intent = await _locked_intent(connection, intent_id)
            if intent["state"] != "outstanding":
                return _receipt_of(intent)
            await connection.execute(
                "UPDATE integration_intents SET sends_started = sends_started + 1 "
                "WHERE intent_id = %s",
                (intent_id,),
            )
        try:
            await self._mirror(intent["project_id"]).advance(
                intent["repository"],
                intent["candidate_revision"],
                f"refs/heads/{intent['trunk']}",
                expected=intent["expected_trunk"],
                timeout=self._push_timeout,
            )
        except RemoteRejected as refused:
            return await self._refused(intent_id, str(refused)[:500])
        except CheckoutFailed as unanswered:
            return await self._unanswered(intent_id, str(unanswered)[:500])
        return await self._converged(intent_id)

    async def _reconcile(self, intent_id: uuid.UUID) -> IntegrationReceipt:
        async with self._pool.connection() as connection:
            intent = await _intent(connection, intent_id)
        if intent["state"] != "outstanding":
            return _receipt_of(intent)
        candidate, expected = intent["candidate_revision"], intent["expected_trunk"]
        head = await trunk_head(intent["repository"], intent["trunk"])
        contains = head not in (candidate, expected) and await self._mirror(
            intent["project_id"]
        ).remote_ref_contains(intent["repository"], f"refs/heads/{intent['trunk']}", candidate)
        reading = read_trunk(
            head, expected=expected, candidate=candidate, contains_candidate=contains
        )
        if reading is TrunkReading.CONVERGED:
            return await self._converged(intent_id)
        if reading is TrunkReading.CONFLICT:
            return await self._conflicted(
                intent_id,
                f"trunk is at {head}, which is neither the expected head nor the candidate",
            )
        if intent["refusal"] is not None:
            # Refused once already, with an earlier send still unanswered. Sending again
            # would learn nothing; only trunk moving can settle this.
            return IntegrationReceipt(Outcome.PENDING, intent["batch_id"], intent["refusal"])
        return await self._drive(intent_id)

    async def _converged(self, intent_id: uuid.UUID) -> IntegrationReceipt:
        async with self._pool.connection() as connection, connection.transaction():
            located = await _intent(connection, intent_id)
            behaviour, row = await _locked_behaviour(
                connection, located["project_id"], located["behaviour_id"]
            )
            intent = await _locked_intent(connection, intent_id)
            if intent["state"] != "outstanding":
                return _receipt_of(intent)
            behaviour.integrate()
            await save_behaviour(connection, intent["project_id"], behaviour)
            await connection.execute(
                "UPDATE integration_intents SET state = 'integrated' WHERE intent_id = %s",
                (intent_id,),
            )
            await connection.execute(
                "UPDATE review_cycles SET state = 'integrated' WHERE cycle_id = %s",
                (intent["cycle_id"],),
            )
            await append_event(
                connection,
                intent["project_id"],
                "behaviour.integrated",
                subject_id=intent["behaviour_id"],
                payload={
                    "intent_id": str(intent_id),
                    "revision": intent["candidate_revision"],
                    "trunk": intent["trunk"],
                },
                correlation_id=row["correlation_id"],
            )
            return IntegrationReceipt(Outcome.INTEGRATED, intent["batch_id"])

    async def _refused(self, intent_id: uuid.UUID, reason: str) -> IntegrationReceipt:
        async with self._pool.connection() as connection, connection.transaction():
            located = await _intent(connection, intent_id)
            behaviour, row = await _locked_behaviour(
                connection, located["project_id"], located["behaviour_id"]
            )
            intent = await _locked_intent(connection, intent_id)
            if intent["state"] != "outstanding":
                return _receipt_of(intent)
            settles = a_rejection_settles_the_intent(
                sends_started=intent["sends_started"], sends_resolved=intent["sends_resolved"]
            )
            await connection.execute(
                "UPDATE integration_intents SET sends_resolved = sends_resolved + 1, "
                "refusal = %s WHERE intent_id = %s",
                (reason, intent_id),
            )
            if settles:
                return await _park(connection, behaviour, row, intent, "rejected", reason)
            await append_event(
                connection,
                intent["project_id"],
                "integration.refusal_left_unsettled",
                subject_id=intent["behaviour_id"],
                payload={"intent_id": str(intent_id), "reason": reason},
            )
            return IntegrationReceipt(Outcome.PENDING, intent["batch_id"], reason)

    async def _unanswered(self, intent_id: uuid.UUID, reason: str) -> IntegrationReceipt:
        async with self._pool.connection() as connection, connection.transaction():
            intent = await _intent(connection, intent_id)
            await append_event(
                connection,
                intent["project_id"],
                "integration.push_unanswered",
                subject_id=intent["behaviour_id"],
                payload={"intent_id": str(intent_id), "reason": reason},
            )
        return IntegrationReceipt(Outcome.PENDING, intent["batch_id"], reason)

    async def _conflicted(self, intent_id: uuid.UUID, reason: str) -> IntegrationReceipt:
        async with self._pool.connection() as connection, connection.transaction():
            located = await _intent(connection, intent_id)
            behaviour, row = await _locked_behaviour(
                connection, located["project_id"], located["behaviour_id"]
            )
            intent = await _locked_intent(connection, intent_id)
            if intent["state"] != "outstanding":
                return _receipt_of(intent)
            return await _park(connection, behaviour, row, intent, "conflicted", reason)

    async def _answer_again(self, batch: dict[str, Any]) -> IntegrationReceipt:
        """A request seen before gets the answer its first arrival produced, brought up to
        date if that answer was still open."""
        if batch["state"] != "authorised":
            return IntegrationReceipt(
                _OUTCOME_OF_BATCH[batch["state"]], batch["batch_id"], batch["reason"] or ""
            )
        async with self._pool.connection() as connection:
            intent = await (
                await connection.execute(
                    "SELECT intent_id FROM integration_intents WHERE batch_id = %s",
                    (batch["batch_id"],),
                )
            ).fetchone()
        assert intent is not None
        return await self._reconcile(intent["intent_id"])


async def _batch_of_request(
    connection: Connection, project_id: str, behaviour_id: str, request_id: str
) -> dict[str, Any] | None:
    return await (
        await connection.execute(
            "SELECT batch_id, state, reason FROM check_batches WHERE project_id = %s "
            "AND behaviour_id = %s AND request_id = %s",
            (project_id, behaviour_id, request_id),
        )
    ).fetchone()


async def _locked_behaviour(
    connection: Connection, project_id: str, behaviour_id: str
) -> tuple[Behaviour, dict[str, Any]]:
    row = await (
        await connection.execute(
            "SELECT * FROM behaviours WHERE project_id = %s AND behaviour_id = %s FOR UPDATE",
            (project_id, behaviour_id),
        )
    ).fetchone()
    if row is None:
        raise LookupError(f"unknown behaviour {behaviour_id!r} in project {project_id!r}")
    return load_behaviour(row), row


async def _locked_project(connection: Connection, project_id: str) -> dict[str, Any]:
    """Taken after the behaviour's lock, always in that order. It holds the check policy
    and the set of integrated behaviours still while a batch is started or settled."""
    row = await (
        await connection.execute(
            "SELECT check_policy_version FROM projects WHERE project_id = %s FOR NO KEY UPDATE",
            (project_id,),
        )
    ).fetchone()
    assert row is not None
    return row


async def _live_cycle(
    connection: Connection, project_id: str, behaviour_id: str
) -> dict[str, Any] | None:
    return await (
        await connection.execute(
            "SELECT c.*, red.revision AS red_revision, green.runtime_identity AS green_runtime "
            "FROM review_cycles c JOIN executions red ON red.execution_id = c.red_execution_id "
            "JOIN executions green ON green.execution_id = c.green_execution_id "
            "WHERE c.project_id = %s AND c.behaviour_id = %s AND c.state IN ('open', 'satisfied')",
            (project_id, behaviour_id),
        )
    ).fetchone()


async def _outstanding_intent(connection: Connection, project_id: str) -> dict[str, Any] | None:
    return await (
        await connection.execute(
            "SELECT intent_id, behaviour_id FROM integration_intents "
            "WHERE project_id = %s AND state = 'outstanding'",
            (project_id,),
        )
    ).fetchone()


async def _regression_set(connection: Connection, project_id: str) -> list[CheckContract]:
    """Everything a candidate must still satisfy: the project's required checks and the
    expectation of every behaviour already on trunk."""
    required = await (
        await connection.execute(
            "SELECT contract FROM required_checks WHERE project_id = %s ORDER BY contract_id",
            (project_id,),
        )
    ).fetchall()
    integrated = await (
        await connection.execute(
            "SELECT contract FROM behaviours WHERE project_id = %s "
            "AND state IN ('integrated', 'completed') ORDER BY behaviour_id",
            (project_id,),
        )
    ).fetchall()
    return [CheckContract.from_dict(row["contract"]) for row in required] + [
        ProcessContract.from_dict(row["contract"]).as_check() for row in integrated
    ]


async def _insert_batch(connection: Connection, batch: dict[str, Any], *, state: str) -> None:
    await connection.execute(
        "INSERT INTO check_batches (batch_id, project_id, behaviour_id, request_id, cycle_id, "
        "candidate_revision, expected_trunk, check_policy_version, regression_digest, members, "
        "state) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            batch["batch_id"],
            batch["project_id"],
            batch["behaviour_id"],
            batch["request_id"],
            batch["cycle_id"],
            batch["candidate_revision"],
            batch["expected_trunk"],
            batch["check_policy_version"],
            batch["regression_digest"],
            Jsonb([check.to_dict() for check in batch["members"]]),
            state,
        ),
    )


async def _why_stale(
    connection: Connection,
    batch: dict[str, Any],
    behaviour: Behaviour,
    row: dict[str, Any],
    cycle: dict[str, Any] | None,
    project: dict[str, Any],
) -> str:
    """Why this batch may no longer decide anything, or an empty string if it still may.
    Everything the batch relied on is looked at again, under the locks."""
    if (
        behaviour.state is not BehaviourState.REVIEW
        or cycle is None
        or cycle["cycle_id"] != batch["cycle_id"]
        or cycle["state"] != "satisfied"
    ):
        return "the review this batch was checking is no longer the current one"
    if (
        row["red_execution_id"] != cycle["red_execution_id"]
        or row["candidate_revision"] != cycle["candidate_revision"]
    ):
        return "the red or the candidate on record is no longer the one that was reviewed"
    if (await gate_of(connection, cycle)).state is not GateState.SATISFIED:
        return "the review gate is no longer satisfied"
    checks = await _regression_set(connection, batch["project_id"])
    if regression_digest(project["check_policy_version"], checks) != batch["regression_digest"]:
        return "the checks required of a candidate changed while this batch was running"
    if await _outstanding_intent(connection, batch["project_id"]) is not None:
        return "another integration of this project was authorised meanwhile"
    return ""


async def _close_batch(
    connection: Connection,
    batch: dict[str, Any],
    state: str,
    reason: str | None,
    *,
    event: bool = True,
) -> None:
    await connection.execute(
        "UPDATE check_batches SET state = %s, reason = %s WHERE batch_id = %s",
        (state, reason, batch["batch_id"]),
    )
    if event:
        await append_event(
            connection,
            batch["project_id"],
            f"integration.batch_{state}",
            subject_id=batch["behaviour_id"],
            payload={"batch_id": str(batch["batch_id"]), "reason": reason},
        )


_INTENT_QUERY = (
    "SELECT i.*, p.repository, p.trunk FROM integration_intents i "
    "JOIN projects p USING (project_id) WHERE i.intent_id = %s"
)


async def _intent(connection: Connection, intent_id: uuid.UUID) -> dict[str, Any]:
    row = await (await connection.execute(_INTENT_QUERY, (intent_id,))).fetchone()
    assert row is not None
    return row


async def _locked_intent(connection: Connection, intent_id: uuid.UUID) -> dict[str, Any]:
    row = await (
        await connection.execute(_INTENT_QUERY + " FOR UPDATE OF i", (intent_id,))
    ).fetchone()
    assert row is not None
    return row


async def _park(
    connection: Connection,
    behaviour: Behaviour,
    row: dict[str, Any],
    intent: dict[str, Any],
    state: str,
    reason: str,
) -> IntegrationReceipt:
    """The update will not happen through this intent. Park the behaviour for a person
    and free the project for other integrations."""
    behaviour.park()
    await save_behaviour(connection, intent["project_id"], behaviour)
    await connection.execute(
        "UPDATE integration_intents SET state = %s, reason = %s WHERE intent_id = %s",
        (state, reason, intent["intent_id"]),
    )
    await append_event(
        connection,
        intent["project_id"],
        f"integration.{state}",
        subject_id=intent["behaviour_id"],
        payload={"intent_id": str(intent["intent_id"]), "reason": reason},
        correlation_id=row["correlation_id"],
    )
    return IntegrationReceipt(Outcome(state), intent["batch_id"], reason)


def _receipt_of(intent: dict[str, Any]) -> IntegrationReceipt:
    return IntegrationReceipt(
        _OUTCOME_OF_INTENT[intent["state"]], intent["batch_id"], intent["reason"] or ""
    )
