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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Concatenate

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from relay.application.assignments import (
    INTEGRATE,
    REMEDIATE,
    announce_again,
    owe,
    retire,
    take_up,
)
from relay.application.behaviours import (
    CONTROL_PLANE,
    load_behaviour,
    record_execution,
    save_behaviour,
)
from relay.application.closure import (
    CANCELLED,
    closure_of_behaviour,
    lock_gate,
    require_open,
    story_basis,
    what_ended,
)
from relay.application.final_checks import Completion
from relay.application.reviews import gate_of
from relay.application.stories import evaluate_story_gate, lock_story, require_story_at_work
from relay.application.supersessions import (
    add_obligation,
    ended_by_an_ordinary_integration,
    regression_set,
    replaced_under,
    reserve,
    settle_with_the_integration,
)
from relay.domain.behaviour import Behaviour, BehaviourState
from relay.domain.evidence import CheckContract, Execution, is_gate_bearing
from relay.domain.identifiers import require_identifier
from relay.domain.integration import (
    TrunkReading,
    a_refusal_settles_the_intent,
    read_trunk,
    regression_digest,
)
from relay.domain.review import GateState
from relay.git.checkout import CheckoutFailed, NotStarted, RemoteRejected, trunk_head
from relay.git.mirror import Mirror, NotFastForward
from relay.persistence.database import Connection, Pool
from relay.persistence.ledger import append_event
from relay.testing.oracle import ProcessOracle
from relay.transport.envelope import Envelope, MessageKind


class IntegrationRefused(Exception):
    """The request cannot be acted on as things stand. Nothing was run or changed."""


class Outcome(StrEnum):
    INTEGRATED = "integrated"
    CHECKS_FAILED = "checks_failed"
    BASE_MOVED = "base_moved"
    RUNTIME_CHANGED = "runtime_changed"
    # The batch finished but may not decide anything: another request, a change of
    # policy or another integration got there first. Its runs are kept as evidence.
    SUPERSEDED = "superseded"
    IN_PROGRESS = "in_progress"
    BUSY = "busy"
    # The checks ran, and the feature was paused before they could decide anything.
    HELD = "held"
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
    "held": Outcome.HELD,
    "interrupted": Outcome.HELD,
    "runtime_changed": Outcome.RUNTIME_CHANGED,
}
_OUTCOME_OF_INTENT = {
    "outstanding": Outcome.PENDING,
    "integrated": Outcome.INTEGRATED,
    "rejected": Outcome.REJECTED,
    "conflicted": Outcome.CONFLICTED,
}


def _then_wake[**P](
    settle: Callable[Concatenate[Integration, uuid.UUID, P], Awaitable[IntegrationReceipt]],
) -> Callable[Concatenate[Integration, uuid.UUID, P], Awaitable[IntegrationReceipt]]:
    """After a method that settles an intent, or finds it settled, has returned, and so
    after the settlement has committed, take up what waited on this project's pushes.
    Finding it settled counts: whoever settled it may have been cut short before this.
    Looking again changes nothing if there is nothing owed."""

    async def settled_and_woken(
        self: Integration, intent_id: uuid.UUID, /, *arguments: P.args, **named: P.kwargs
    ) -> IntegrationReceipt:
        receipt = await settle(self, intent_id, *arguments, **named)
        if receipt.outcome is not Outcome.PENDING:
            async with self._pool.connection() as connection:
                intent = await _intent(connection, intent_id)
            await self.completion.take_up_what_is_owed(intent["project_id"])
        return receipt

    return settled_and_woken


class Integration:
    def __init__(
        self, pool: Pool, mirrors: Path, oracle: ProcessOracle, push_timeout: float = 120.0
    ) -> None:
        self._pool = pool
        self._mirrors = mirrors
        self._oracle = oracle
        # How long any one exchange with the repository may take, a read as much as a push.
        self._push_timeout = push_timeout
        # A feature's final checks put off behind a push are taken up by whichever
        # control plane settles that push, with the oracle it observes its own checks by.
        self.completion = Completion(pool, oracle, push_timeout)

    def _mirror(self, project_id: str) -> Mirror:
        return Mirror(self._mirrors / f"{require_identifier('project', project_id)}.git")

    async def require_check(self, project_id: str, check: CheckContract) -> None:
        """From now on every candidate of this project must pass this check before it can
        reach trunk. A batch already running was started under the old set and will not
        be allowed to decide anything."""
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
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
            await add_obligation(connection, project_id, "check", check)
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
        it never starts the checks again.

        ActionRefused: the behaviour's story is not being worked on, so nothing may start.
        IntegrationRefused: the story is open but this candidate is not ready."""
        require_identifier("request", request_id)
        self._require_gate_bearing_checks()
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
        return await self._finish(
            await self._start_batch(project_id, behaviour_id, request_id, head)
        )

    async def take_up(self, assignment_id: uuid.UUID) -> IntegrationReceipt | None:
        """Take up an integration that is owed: the first of a candidate whose review
        gate was satisfied, or the next generation of one that was held or cut short. A
        first integration whose cycle is no longer the behaviour's satisfied one is
        retired, and nothing starts."""
        async with self._pool.connection() as connection, connection.transaction():
            owed = await (
                await connection.execute(
                    "SELECT project_id, behaviour_id, issued_on FROM assignments "
                    "WHERE assignment_id = %s AND kind = 'integrate' AND state = 'wanted'",
                    (assignment_id,),
                )
            ).fetchone()
            if owed is None:
                return None
            if "batch_id" not in owed["issued_on"]:
                await lock_gate(connection, owed["project_id"])
                cycle = await _live_cycle(connection, owed["project_id"], owed["behaviour_id"])
                if (
                    cycle is None
                    or str(cycle["cycle_id"]) != owed["issued_on"]["cycle_id"]
                    or cycle["state"] != "satisfied"
                ):
                    await retire(connection, assignment_id)
                    return None
        if "batch_id" in owed["issued_on"]:
            return await self.continue_integration(assignment_id)
        cycle_id = str(owed["issued_on"]["cycle_id"])
        self._require_gate_bearing_checks()
        async with self._pool.connection() as connection:
            source = await (
                await connection.execute(
                    "SELECT repository, trunk FROM projects WHERE project_id = %s",
                    (owed["project_id"],),
                )
            ).fetchone()
        assert source is not None
        head = await trunk_head(source["repository"], source["trunk"], timeout=self._push_timeout)
        # The cycle is looked at again where the batch is made: what was satisfied a
        # moment ago may have been replaced while the repository was being asked.
        return await self._finish(
            await self._start_batch(
                owed["project_id"],
                owed["behaviour_id"],
                f"cycle-{cycle_id}",
                head,
                first=(assignment_id, cycle_id),
            )
        )

    async def continue_integration(self, assignment_id: uuid.UUID) -> IntegrationReceipt:
        """Take up an integration whose checks were held by a pause or cut short by a
        restart: start the next generation of the same request, if what it would
        continue is still the latest batch of the behaviour's live, satisfied cycle.
        Otherwise the assignment is retired and nothing starts."""
        self._require_gate_bearing_checks()
        async with self._pool.connection() as connection:
            owed = await (
                await connection.execute(
                    "SELECT a.project_id, a.behaviour_id, b.request_id, p.repository, p.trunk "
                    "FROM assignments a JOIN check_batches b "
                    "ON b.batch_id = (a.issued_on->>'batch_id')::uuid "
                    "JOIN projects p ON p.project_id = a.project_id "
                    "WHERE a.assignment_id = %s AND a.kind = 'integrate'",
                    (assignment_id,),
                )
            ).fetchone()
            if owed is not None:
                # Before the repository is asked anything: a closed feature is not worth
                # a call that may be slow. The transaction that takes the work up looks
                # again, under the gate lock.
                require_open(
                    await closure_of_behaviour(connection, owed["project_id"], owed["behaviour_id"])
                )
        if owed is None:
            raise LookupError(f"no integration is owed under {assignment_id}")
        head = await trunk_head(owed["repository"], owed["trunk"], timeout=self._push_timeout)
        return await self._finish(
            await self._start_batch(
                owed["project_id"],
                owed["behaviour_id"],
                owed["request_id"],
                head,
                successor=assignment_id,
            )
        )

    @property
    def oracle(self) -> ProcessOracle:
        return self._oracle

    @property
    def remote_timeout(self) -> float:
        return self._push_timeout

    def _require_gate_bearing_checks(self) -> None:
        """Before anything is taken up or launched, by either way in."""
        if not is_gate_bearing(self._oracle.isolation.name):
            raise IntegrationRefused(
                f"{self._oracle.isolation.name} execution cannot open a gate; "
                "checks must be observed in a container"
            )

    async def _finish(self, started: dict[str, Any] | IntegrationReceipt) -> IntegrationReceipt:
        if isinstance(started, IntegrationReceipt):
            return started
        # Taken before the runner is handed anything: which batch is settled or abandoned
        # is not for the code that ran the checks to say.
        batch_id: uuid.UUID = started["batch_id"]
        try:
            executions = await self._run_checks(started)
        except BaseException:
            await self._abandon(batch_id)
            raise
        try:
            settled = await self._settle_batch(batch_id, executions)
        except BaseException:
            # Nothing was decided if the batch is still running; if an intent was
            # committed, the batch is no longer running and the intent owns what follows.
            await self._abandon(batch_id)
            raise
        if isinstance(settled, IntegrationReceipt):
            return settled
        return await self._drive(settled)

    async def reconcile(self, *, then_completions: bool = True) -> int:
        """Settle every authorised integration whose outcome is not yet on record, from
        what trunk holds now. Run on start and whenever a push went unanswered. A caller
        that goes on to take up completions itself says so, and they are left to it."""
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
        if not then_completions:
            return settled
        # A settlement whose own wake-up was cut short has no outstanding intent left to
        # be found by. What it freed is still on record as owed, and is taken up here.
        async with self._pool.connection() as connection:
            waiting = await (
                await connection.execute(
                    "SELECT DISTINCT project_id FROM assignments WHERE kind = 'complete' "
                    "AND state = 'wanted' ORDER BY project_id"
                )
            ).fetchall()
        for row in waiting:
            await self.completion.take_up_what_is_owed(row["project_id"])
        return settled

    async def retry_integration(self, project_id: str, behaviour_id: str, reason: str) -> None:
        """Put a behaviour whose integration the repository refused back where it can be
        integrated again. A person decides this, once the cause has been dealt with."""
        if not reason.strip():
            raise ValueError("retrying an integration needs a stated reason")
        async with self._pool.connection() as connection, connection.transaction():
            behaviour, _ = await _locked_behaviour(connection, project_id, behaviour_id)
            require_open(await closure_of_behaviour(connection, project_id, behaviour_id))
            last = await (
                await connection.execute(
                    "SELECT i.state, c.state AS cycle_state FROM integration_intents i "
                    "JOIN review_cycles c USING (cycle_id) WHERE i.project_id = %s "
                    "AND i.behaviour_id = %s ORDER BY i.created_at DESC LIMIT 1",
                    (project_id, behaviour_id),
                )
            ).fetchone()
            if last is None or last["state"] != "rejected" or last["cycle_state"] != "satisfied":
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
        self,
        project_id: str,
        behaviour_id: str,
        request_id: str,
        head: str,
        successor: uuid.UUID | None = None,
        first: tuple[uuid.UUID, str] | None = None,
    ) -> dict[str, Any] | IntegrationReceipt:
        answer_again: dict[str, Any] | None = None
        reconcile: uuid.UUID | None = None
        generation = 1
        async with self._pool.connection() as connection, connection.transaction():
            behaviour, row = await _locked_behaviour(connection, project_id, behaviour_id)
            if successor is None:
                answer_again = await _batch_of_request(
                    connection, project_id, behaviour_id, request_id
                )
            else:
                parent = await _parent_to_succeed(connection, successor)
                if isinstance(parent, IntegrationReceipt):
                    return parent
                if parent is None:  # taken up already: the request has its answer
                    answer_again = await _batch_of_request(
                        connection, project_id, behaviour_id, request_id
                    )
                else:
                    generation = parent["generation"] + 1
            if answer_again is None:
                await require_story_at_work(
                    connection, project_id, row, entering_design=False, on_its_contract=True
                )
                cycle = await _live_cycle(connection, project_id, behaviour_id)
                if first is not None and (
                    cycle is None
                    or str(cycle["cycle_id"]) != first[1]
                    or cycle["state"] != "satisfied"
                ):
                    # Taken up for one cycle, and that is no longer the behaviour's
                    # satisfied one: it has been replaced since this was looked at.
                    # Nothing is started for whatever is there now under this request;
                    # that cycle is owed its own.
                    await retire(connection, first[0])
                    return IntegrationReceipt(
                        Outcome.SUPERSEDED,
                        reason="the candidate this integration was owed for is no longer "
                        "the one whose review gate is satisfied",
                    )
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
                    checks, excused_under = await regression_set(
                        connection, project_id, behaviour_id
                    )
                    batch = {
                        "batch_id": uuid.uuid7(),
                        "project_id": project_id,
                        "behaviour_id": behaviour_id,
                        "request_id": request_id,
                        "generation": generation,
                        "supersession_id": excused_under,
                        "basis": await story_basis(connection, project_id, row["story_id"]),
                        "cycle_id": cycle["cycle_id"],
                        "candidate_revision": cycle["candidate_revision"],
                        "expected_trunk": head,
                        "check_policy_version": project["check_policy_version"],
                        "regression_digest": regression_digest(
                            project["check_policy_version"], checks
                        ),
                        "members": checks,
                    }
                    if successor is not None:
                        await take_up(connection, successor, CONTROL_PLANE)
                    # The first integration owed for this cycle is this one, whoever
                    # asked for it: its grant and its first effect are made together.
                    await connection.execute(
                        "UPDATE assignments SET state = 'done', grant_id = gen_random_uuid(), "
                        "granted_to = %s, first_effect_at = now() WHERE project_id = %s "
                        "AND behaviour_id = %s AND kind = 'integrate' AND state = 'wanted' "
                        "AND issued_on->>'cycle_id' = %s AND NOT issued_on ? 'batch_id'",
                        (CONTROL_PLANE, project_id, behaviour_id, str(cycle["cycle_id"])),
                    )
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
        """The checks could not be completed. An integration is the control plane's to
        see through, the first generation as much as any later one: it stays owed."""
        async with self._pool.connection() as connection, connection.transaction():
            batch = await (
                await connection.execute(
                    "SELECT project_id, behaviour_id, generation FROM check_batches "
                    "WHERE batch_id = %s",
                    (batch_id,),
                )
            ).fetchone()
            assert batch is not None
            await _close_unfinished(
                connection, batch["project_id"], batch["behaviour_id"], batch_id, "running"
            )

    async def _settle_batch(
        self, batch_id: uuid.UUID, executions: list[tuple[CheckContract, Execution]]
    ) -> uuid.UUID | IntegrationReceipt:
        """Decide what a finished batch means. It may only send work back or authorise an
        integration if nothing it relied on has changed since it started.

        Everything decided here is decided from the batch as recorded and locked, never
        from what the code that ran the checks was holding: what gets authorised must be
        what was checked."""
        async with self._pool.connection() as connection, connection.transaction():
            located = await (
                await connection.execute(
                    "SELECT project_id, behaviour_id FROM check_batches WHERE batch_id = %s",
                    (batch_id,),
                )
            ).fetchone()
            assert located is not None
            project_id, behaviour_id = located["project_id"], located["behaviour_id"]
            behaviour, row = await _locked_behaviour(connection, project_id, behaviour_id)
            project = await _locked_project(connection, project_id)
            cycle = await _live_cycle(connection, project_id, behaviour_id)
            batch = await (
                await connection.execute(
                    "SELECT * FROM check_batches WHERE batch_id = %s FOR UPDATE", (batch_id,)
                )
            ).fetchone()
            assert batch is not None
            current = batch
            cycle_id = batch["cycle_id"]
            if incomplete := _why_incomplete(current, executions):
                # What came back is not a run of every check on the candidate, so none
                # of it is taken as evidence of anything.
                if current["state"] == "running":
                    await _discard(connection, batch, incomplete)
                return IntegrationReceipt(Outcome.SUPERSEDED, batch_id, incomplete)
            runs = []
            for check, execution in executions:
                execution_id = await record_execution(
                    connection,
                    project_id,
                    behaviour_id,
                    "check",
                    check.digest,
                    execution,
                    batch["basis"],
                )
                satisfied = check.is_satisfied_by(execution)
                await connection.execute(
                    "INSERT INTO check_runs (batch_id, contract_digest, contract_id, "
                    "execution_id, satisfied) VALUES (%s, %s, %s, %s, %s)",
                    (batch_id, check.digest, check.contract_id, execution_id, satisfied),
                )
                runs.append((check, execution, execution_id, satisfied))
            if current["state"] != "running":
                return IntegrationReceipt(Outcome.SUPERSEDED, batch_id)
            closed_by = await closure_of_behaviour(connection, project_id, behaviour_id)
            if closed_by is not None:
                # The one boundary for trunk: a closure that committed before this
                # transaction means no intent. What the checks saw is kept.
                state = "abandoned" if closed_by == CANCELLED else "held"
                ended = (
                    await what_ended(connection, project_id, behaviour_id)
                    if closed_by == CANCELLED
                    else f"the feature was {closed_by}"
                )
                reason = f"{ended} before these checks could decide"
                await _close_batch(connection, batch, state, reason)
                return IntegrationReceipt(_OUTCOME_OF_BATCH[state], batch_id, reason)
            stale = await _why_stale(connection, batch, behaviour, row, cycle, project)
            if stale:
                await _discard(connection, batch, stale)
                return IntegrationReceipt(Outcome.SUPERSEDED, batch_id, stale)
            assert cycle is not None
            if any(
                execution.observation.is_eligible
                and execution.runtime_identity != cycle["green_runtime"]
                for _, execution, _, _ in runs
            ):
                reason = "the runtime is no longer the one the green was observed in"
                await _close_batch(connection, batch, "runtime_changed", reason)
                await self._reopen(connection, behaviour, cycle, reason, {})
                return IntegrationReceipt(Outcome.RUNTIME_CHANGED, batch_id, reason)
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
                "batch_id, candidate_revision, expected_trunk, repository, trunk, "
                "supersession_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    intent_id,
                    project_id,
                    behaviour_id,
                    cycle_id,
                    batch_id,
                    batch["candidate_revision"],
                    batch["expected_trunk"],
                    project["repository"],
                    project["trunk"],
                    batch["supersession_id"],
                ),
            )
            if batch["supersession_id"] is not None:
                # This push was checked without the obligation it replaces. From here
                # that exemption is this integration's, whatever is decided meanwhile.
                await reserve(connection, batch["supersession_id"], intent_id)
            else:
                await ended_by_an_ordinary_integration(
                    connection, batch["project_id"], batch["behaviour_id"]
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
        await owe(
            connection,
            REMEDIATE,
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
            issued_on={"cycle_id": str(cycle_id)},
        )

    @_then_wake
    async def _drive(self, intent_id: uuid.UUID) -> IntegrationReceipt:
        return await self._send(intent_id)

    async def _send(self, intent_id: uuid.UUID) -> IntegrationReceipt:
        """Send the authorised update. Everything that can be settled on this side is
        settled first; the send is then counted, in its own transaction, immediately
        before it leaves. If this process dies after that, the record still says a push
        may be on its way."""
        async with self._pool.connection() as connection:
            intent = await _intent(connection, intent_id)
        sent = False

        async def count_the_send() -> bool:
            nonlocal sent
            async with self._pool.connection() as connection, connection.transaction():
                await lock_gate(connection, intent["project_id"])
                current = await _locked_intent(connection, intent_id)
                # Looked at again under the lock: another process may have settled the
                # intent, or been refused, since this one decided to send.
                if current["state"] != "outstanding" or current["refusal"] is not None:
                    return False
                await connection.execute(
                    "UPDATE integration_intents SET sends_started = sends_started + 1 "
                    "WHERE intent_id = %s",
                    (intent_id,),
                )
            sent = True
            return True

        try:
            await self._recover_the_pin(intent)
            await self._mirror(intent["project_id"]).advance(
                intent["repository"],
                intent["candidate_revision"],
                f"refs/heads/{intent['trunk']}",
                expected=intent["expected_trunk"],
                timeout=self._push_timeout,
                about_to_send=count_the_send,
            )
        except NotFastForward as never_sendable:
            return await self._refused(intent_id, str(never_sendable), sent=False)
        except RemoteRejected as refused:
            return await self._refused_by_the_repository(intent, str(refused)[:500])
        except NotStarted as never_ran:
            if sent:
                return await self._send_never_left(intent_id, str(never_ran)[:500])
            return IntegrationReceipt(Outcome.PENDING, intent["batch_id"], str(never_ran)[:500])
        except CheckoutFailed as failure:
            if sent:
                return await self._unanswered(intent_id, str(failure)[:500])
            # Nothing left this machine, so there is no send to wait for.
            return IntegrationReceipt(Outcome.PENDING, intent["batch_id"], str(failure)[:500])
        if not sent:
            async with self._pool.connection() as connection:
                return _receipt_of(await _intent(connection, intent_id))
        return await self._converged(intent_id)

    async def _recover_the_pin(self, intent: dict[str, Any]) -> None:
        """A mirror can be lost between authorisation and the push. The candidate was
        published for review under its own ref, and that ref can give it back."""
        mirror = self._mirror(intent["project_id"])
        if await mirror.holds(intent["candidate_revision"]):
            return
        async with self._pool.connection() as connection:
            cycle = await (
                await connection.execute(
                    "SELECT candidate_ref FROM review_cycles WHERE cycle_id = %s",
                    (intent["cycle_id"],),
                )
            ).fetchone()
        assert cycle is not None
        if not await mirror.recover(
            intent["repository"],
            intent["candidate_revision"],
            [cycle["candidate_ref"]],
            timeout=self._push_timeout,
        ):
            raise CheckoutFailed("the authorised commit can no longer be obtained")

    async def _send_never_left(self, intent_id: uuid.UUID, reason: str) -> IntegrationReceipt:
        """The send was counted and then Git is known never to have run. That send does
        not exist, so it is answered. If it was the last one unanswered and a refusal is
        already on record, the intent is settled by that refusal now."""
        async with self._pool.connection() as connection, connection.transaction():
            located = await _intent(connection, intent_id)
            behaviour, row = await _locked_behaviour(
                connection, located["project_id"], located["behaviour_id"]
            )
            intent = await _locked_intent(connection, intent_id)
            if intent["state"] != "outstanding":
                return _receipt_of(intent)
            answered = intent["sends_resolved"] + 1
            await connection.execute(
                "UPDATE integration_intents SET sends_resolved = %s WHERE intent_id = %s",
                (answered, intent_id),
            )
            await append_event(
                connection,
                intent["project_id"],
                "integration.push_not_started",
                subject_id=intent["behaviour_id"],
                payload={"intent_id": str(intent_id), "reason": reason},
            )
            if intent["refusal"] is not None and a_refusal_settles_the_intent(
                sends_started=intent["sends_started"], sends_answered=answered
            ):
                return await _park(connection, behaviour, row, intent, intent["refusal"])
        return IntegrationReceipt(Outcome.PENDING, intent["batch_id"], reason)

    async def _refused_by_the_repository(
        self, intent: dict[str, Any], reason: str
    ) -> IntegrationReceipt:
        """A refusal may only mean trunk has moved. One fresh reading says whether it has:
        if so, no push under this intent can land any more, whatever is still in flight."""
        try:
            reading, head = await self._read_trunk(intent)
        except CheckoutFailed:
            reading, head = TrunkReading.UNVERIFIABLE, ""
        if reading is TrunkReading.CONVERGED:
            return await self._converged(intent["intent_id"])
        if reading is TrunkReading.CONFLICT:
            return await self._conflicted(intent["intent_id"], head)
        return await self._refused(intent["intent_id"], reason, sent=True)

    async def _read_trunk(self, intent: dict[str, Any]) -> tuple[TrunkReading, str]:
        seen = await self._mirror(intent["project_id"]).read_ref(
            intent["repository"],
            f"refs/heads/{intent['trunk']}",
            base=intent["expected_trunk"],
            timeout=self._push_timeout,
        )
        reading = read_trunk(
            seen.head,
            expected=intent["expected_trunk"],
            candidate=intent["candidate_revision"],
            beyond_expected=seen.beyond_base,
        )
        return reading, seen.head

    @_then_wake
    async def _reconcile(self, intent_id: uuid.UUID) -> IntegrationReceipt:
        async with self._pool.connection() as connection:
            intent = await _intent(connection, intent_id)
        if intent["state"] != "outstanding":
            return _receipt_of(intent)
        reading, head = await self._read_trunk(intent)
        if reading is TrunkReading.CONVERGED:
            return await self._converged(intent_id)
        if reading is TrunkReading.CONFLICT:
            return await self._conflicted(intent_id, head)
        if reading is TrunkReading.UNVERIFIABLE:
            return IntegrationReceipt(
                Outcome.PENDING,
                intent["batch_id"],
                f"trunk was read at {head}, which is not at or beyond the expected head",
            )
        if intent["refusal"] is not None:
            # Refused once already, with an earlier send still unanswered. Sending again
            # would learn nothing; only trunk moving can settle this.
            return IntegrationReceipt(Outcome.PENDING, intent["batch_id"], intent["refusal"])
        return await self._send(intent_id)

    async def _converged(self, intent_id: uuid.UUID) -> IntegrationReceipt:
        async with self._pool.connection() as connection, connection.transaction():
            located = await _intent(connection, intent_id)
            behaviour, row = await _locked_behaviour(
                connection, located["project_id"], located["behaviour_id"]
            )
            # If this push replaces a behaviour, that one is written too. Its row is
            # taken now, with the other behaviour and before any story: a reconfirmation
            # holds a behaviour and then asks for its story, and must never meet a
            # settlement that holds the story and asks for the behaviour. (Which
            # authority an intent carries never changes once it is authorised.)
            replaced = await replaced_under(connection, located["supersession_id"])
            if replaced is not None:
                await _locked_behaviour(connection, located["project_id"], replaced)
            # The story is locked here, before anything writes to the project row (the
            # ledger does): behaviour, story, project is the order everyone keeps, and
            # the gate evaluation below needs the story.
            await lock_story(connection, located["project_id"], row["story_id"])
            intent = await _locked_intent(connection, intent_id)
            if intent["state"] != "outstanding":
                return _receipt_of(intent)
            behaviour.integrate()
            await save_behaviour(connection, intent["project_id"], behaviour)
            await connection.execute(
                "UPDATE integration_intents SET state = 'integrated' WHERE intent_id = %s",
                (intent_id,),
            )
            # What it shows is now something every later candidate must still satisfy;
            # and if it was let in as a replacement, what it replaces is retired.
            assert behaviour.contract is not None
            await add_obligation(
                connection,
                intent["project_id"],
                "behaviour",
                behaviour.contract.as_check(),
                intent["behaviour_id"],
            )
            await settle_with_the_integration(connection, intent, integrated=True)
            # A reconfirmation that could not be claimed while this was outstanding can be
            # now. After the retirement above, so that what it withdrew is not sent again.
            await announce_again(connection, intent["project_id"])
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
            # The last behaviour of a sealed list reaching trunk is what opens its
            # story's gate, here, in the transaction that records it.
            await evaluate_story_gate(connection, intent["project_id"], row["story_id"])
            return IntegrationReceipt(Outcome.INTEGRATED, intent["batch_id"])

    async def _refused(
        self, intent_id: uuid.UUID, reason: str, *, sent: bool
    ) -> IntegrationReceipt:
        async with self._pool.connection() as connection, connection.transaction():
            located = await _intent(connection, intent_id)
            behaviour, row = await _locked_behaviour(
                connection, located["project_id"], located["behaviour_id"]
            )
            intent = await _locked_intent(connection, intent_id)
            if intent["state"] != "outstanding":
                return _receipt_of(intent)
            answered = intent["sends_resolved"] + (1 if sent else 0)
            await connection.execute(
                "UPDATE integration_intents SET sends_resolved = %s, refusal = %s "
                "WHERE intent_id = %s",
                (answered, reason, intent_id),
            )
            if a_refusal_settles_the_intent(
                sends_started=intent["sends_started"], sends_answered=answered
            ):
                return await _park(connection, behaviour, row, intent, reason)
            await append_event(
                connection,
                intent["project_id"],
                "integration.refusal_left_unsettled",
                subject_id=intent["behaviour_id"],
                payload={
                    "intent_id": str(intent_id),
                    "expected_trunk": intent["expected_trunk"],
                    "candidate_revision": intent["candidate_revision"],
                    "reason": reason,
                    "waiting_for": "an earlier push that was never answered and may still land",
                },
            )
            return IntegrationReceipt(Outcome.PENDING, intent["batch_id"], reason)

    async def _unanswered(self, intent_id: uuid.UUID, reason: str) -> IntegrationReceipt:
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, (await _intent(connection, intent_id))["project_id"])
            intent = await _locked_intent(connection, intent_id)
            if intent["state"] != "outstanding":
                # Settled by someone else while this send was waiting for its answer.
                return _receipt_of(intent)
            await append_event(
                connection,
                intent["project_id"],
                "integration.push_unanswered",
                subject_id=intent["behaviour_id"],
                payload={"intent_id": str(intent_id), "reason": reason},
            )
        return IntegrationReceipt(Outcome.PENDING, intent["batch_id"], reason)

    async def _conflicted(self, intent_id: uuid.UUID, head: str) -> IntegrationReceipt:
        """Trunk has moved on to something that is not the candidate. No push under this
        intent can land now, so the intent is over, and the candidate must be observed
        again on what trunk has become."""
        reason = f"trunk moved on to {head}, which is not the candidate"
        async with self._pool.connection() as connection, connection.transaction():
            located = await _intent(connection, intent_id)
            behaviour, row = await _locked_behaviour(
                connection, located["project_id"], located["behaviour_id"]
            )
            intent = await _locked_intent(connection, intent_id)
            if intent["state"] != "outstanding":
                return _receipt_of(intent)
            await connection.execute(
                "UPDATE integration_intents SET state = 'conflicted', reason = %s "
                "WHERE intent_id = %s",
                (reason, intent_id),
            )
            # A reconfirmation that could not be claimed while this was outstanding can be now.
            await announce_again(connection, intent["project_id"])
            await settle_with_the_integration(connection, intent, integrated=False)
            await append_event(
                connection,
                intent["project_id"],
                "integration.conflicted",
                subject_id=intent["behaviour_id"],
                payload={"intent_id": str(intent_id), "reason": reason, "trunk": head},
                correlation_id=row["correlation_id"],
            )
            await self._reopen(connection, behaviour, intent, reason, {"trunk": head})
            return IntegrationReceipt(Outcome.CONFLICTED, intent["batch_id"], reason)

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


async def hand_on_unfinished_batches(
    connection: Connection, project_id: str, *, starting: bool
) -> None:
    """Checks that a pause held, or that a process which is gone left running, decide
    nothing. On an open feature each such batch is closed as interrupted and an
    integration is owed in its place. On start, a batch left running on a closed feature
    is held (paused) or abandoned (cancelled). Called under the project's gate lock."""
    unfinished = await (
        await connection.execute(
            "SELECT k.batch_id, k.behaviour_id, k.state FROM check_batches k "
            "WHERE k.project_id = %s AND k.state = ANY(%s) ORDER BY k.registered",
            (project_id, ["held", "running"] if starting else ["held"]),
        )
    ).fetchall()
    for found in unfinished:
        await _close_unfinished(
            connection, project_id, found["behaviour_id"], found["batch_id"], found["state"]
        )


async def _close_unfinished(
    connection: Connection,
    project_id: str,
    behaviour_id: str,
    batch_id: uuid.UUID,
    state: str,
    why: str = "these checks did not get to decide",
) -> None:
    """Close one batch that will not decide anything, if it is still in `state`, by what
    its feature now is: open, an integration is owed in its place; paused, it is held;
    cancelled, it is abandoned."""
    _, row = await _locked_behaviour(connection, project_id, behaviour_id)
    batch = await (
        await connection.execute(
            "SELECT * FROM check_batches WHERE batch_id = %s AND state = %s FOR UPDATE",
            (batch_id, state),
        )
    ).fetchone()
    closed_by = await closure_of_behaviour(connection, project_id, behaviour_id)
    if batch is None or (closed_by is not None and state == "held"):
        return
    if closed_by is not None:
        await _close_batch(
            connection,
            batch,
            "abandoned" if closed_by == CANCELLED else "held",
            f"{why}, and "
            + (
                await what_ended(connection, project_id, behaviour_id)
                if closed_by == CANCELLED
                else f"the feature is {closed_by}"
            ),
        )
        return
    await _close_batch(connection, batch, "interrupted", f"{why}; to be run again")
    await owe(
        connection,
        INTEGRATE,
        Envelope(
            message_kind=MessageKind.COMMAND,
            event_type="integration.requested",
            project_id=project_id,
            behaviour_id=behaviour_id,
            sender=CONTROL_PLANE,
            recipient="integrator",
            idempotency_key=f"integrate:{batch['batch_id']}",
            correlation_id=row["correlation_id"],
            payload={"batch_id": str(batch["batch_id"]), "request_id": batch["request_id"]},
        ),
        issued_on={"batch_id": str(batch["batch_id"]), "cycle_id": str(batch["cycle_id"])},
    )


async def _discard(connection: Connection, batch: dict[str, Any], reason: str) -> None:
    """What these checks saw can no longer decide anything. The integration stays owed,
    whichever generation this was and whoever asked for it: no caller is relied on to
    ask again. Whether it is still worth doing is judged when it is next taken up."""
    await _close_unfinished(
        connection,
        batch["project_id"],
        batch["behaviour_id"],
        batch["batch_id"],
        "running",
        why=reason,
    )


async def _parent_to_succeed(
    connection: Connection, assignment_id: uuid.UUID
) -> dict[str, Any] | IntegrationReceipt | None:
    """The batch this assignment would continue, if it is still worth continuing; None
    if the assignment has been taken up already; a receipt if it has gone stale, in which
    case it is retired here. Called under the behaviour's lock."""
    assignment = await (
        await connection.execute(
            "SELECT * FROM assignments WHERE assignment_id = %s FOR UPDATE", (assignment_id,)
        )
    ).fetchone()
    assert assignment is not None
    if assignment["state"] != "wanted":
        return None
    parent = await (
        await connection.execute(
            "SELECT * FROM check_batches WHERE batch_id = %s",
            (assignment["issued_on"]["batch_id"],),
        )
    ).fetchone()
    assert parent is not None
    cycle = await _live_cycle(connection, parent["project_id"], parent["behaviour_id"])
    # The latest batch of the behaviour's live cycle, whichever cycle that now is: if the
    # cycle was replaced, or newer checks were started on it, that batch is not the parent.
    latest = await (
        await connection.execute(
            "SELECT batch_id FROM check_batches WHERE cycle_id = %s "
            "ORDER BY registered DESC LIMIT 1",
            (cycle["cycle_id"] if cycle else None,),
        )
    ).fetchone()
    if latest is not None and latest["batch_id"] == parent["batch_id"]:
        return parent
    reason = "what these checks would have continued has since been replaced"
    await retire(connection, assignment_id)
    await append_event(
        connection,
        parent["project_id"],
        "integration.successor_stale",
        subject_id=parent["behaviour_id"],
        payload={"batch_id": str(parent["batch_id"]), "assignment_id": str(assignment_id)},
    )
    return IntegrationReceipt(Outcome.SUPERSEDED, parent["batch_id"], reason)


async def _batch_of_request(
    connection: Connection, project_id: str, behaviour_id: str, request_id: str
) -> dict[str, Any] | None:
    return await (
        await connection.execute(
            "SELECT batch_id, state, reason FROM check_batches WHERE project_id = %s "
            "AND behaviour_id = %s AND request_id = %s ORDER BY generation DESC LIMIT 1",
            (project_id, behaviour_id, request_id),
        )
    ).fetchone()


async def _locked_behaviour(
    connection: Connection, project_id: str, behaviour_id: str
) -> tuple[Behaviour, dict[str, Any]]:
    """The gate lock, then the behaviour: the first two locks of every transaction here."""
    await lock_gate(connection, project_id)
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
            "SELECT check_policy_version, repository, trunk FROM projects "
            "WHERE project_id = %s FOR NO KEY UPDATE",
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


def _why_incomplete(
    batch: dict[str, Any], executions: list[tuple[CheckContract, Execution]]
) -> str:
    """Why what the runner returned is not one run of every member of the batch on the
    batch's candidate, or an empty string if it is exactly that."""
    wanted = sorted(CheckContract.from_dict(member).digest for member in batch["members"])
    if sorted(check.digest for check, _ in executions) != wanted:
        return "the runs returned are not one for each check of the batch"
    if any(execution.revision != batch["candidate_revision"] for _, execution in executions):
        return "a run was made on a commit other than the candidate"
    return ""


async def _insert_batch(connection: Connection, batch: dict[str, Any], *, state: str) -> None:
    await connection.execute(
        "INSERT INTO check_batches (batch_id, project_id, behaviour_id, request_id, cycle_id, "
        "candidate_revision, expected_trunk, check_policy_version, regression_digest, members, "
        "state, generation, basis, supersession_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
            batch["generation"],
            batch["basis"],
            batch["supersession_id"],
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
    ):
        return "the review this batch was checking is no longer the current one"
    if (
        row["red_execution_id"] != cycle["red_execution_id"]
        or row["candidate_revision"] != cycle["candidate_revision"]
    ):
        return "the red or the candidate on record is no longer the one that was reviewed"
    if batch["basis"] != await story_basis(connection, batch["project_id"], row["story_id"]):
        return "a decision of the Product Owner's was made while these checks were running"
    if (await gate_of(connection, cycle)).state is not GateState.SATISFIED:
        return "the review gate is no longer satisfied"
    # Recomputed for this behaviour: an authority given, voided or used while the batch
    # ran changes what the batch may decide.
    checks, excused_under = await regression_set(
        connection, batch["project_id"], batch["behaviour_id"]
    )
    if regression_digest(project["check_policy_version"], checks) != batch["regression_digest"]:
        return "the checks required of a candidate changed while this batch was running"
    if excused_under != batch["supersession_id"]:
        # The same checks may run with and without an exemption, when something else
        # asks what the replaced obligation asks. The push would still mean something
        # different: with the authority it retires the obligation, without it it does not.
        return "what this behaviour may replace was decided while this batch was running"
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


_INTENT_QUERY = "SELECT i.* FROM integration_intents i WHERE i.intent_id = %s"


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
    reason: str,
) -> IntegrationReceipt:
    """The repository will not take the update through this intent. Park the behaviour
    for a person and free the project for other integrations."""
    behaviour.park()
    await save_behaviour(connection, intent["project_id"], behaviour)
    await connection.execute(
        "UPDATE integration_intents SET state = 'rejected', reason = %s WHERE intent_id = %s",
        (reason, intent["intent_id"]),
    )
    # A reconfirmation that could not be claimed while this was outstanding can be now.
    await announce_again(connection, intent["project_id"])
    await settle_with_the_integration(connection, intent, integrated=False)
    # A conflict on the candidate ref could not stop this intent while it was owed. The
    # intent is over, so the conflict now stops the cycle being used for another.
    await connection.execute(
        "UPDATE review_cycles SET state = 'blocked', blocked_reason = conflict_observed "
        "WHERE cycle_id = %s AND conflict_observed IS NOT NULL",
        (intent["cycle_id"],),
    )
    await append_event(
        connection,
        intent["project_id"],
        "integration.rejected",
        subject_id=intent["behaviour_id"],
        payload={"intent_id": str(intent["intent_id"]), "reason": reason},
        correlation_id=row["correlation_id"],
    )
    return IntegrationReceipt(Outcome.REJECTED, intent["batch_id"], reason)


def _receipt_of(intent: dict[str, Any]) -> IntegrationReceipt:
    return IntegrationReceipt(
        _OUTCOME_OF_INTENT[intent["state"]],
        intent["batch_id"],
        intent["reason"] or intent["refusal"] or "",
    )
