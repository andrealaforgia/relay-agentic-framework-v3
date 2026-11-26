"""Use cases that move a behaviour through its lifecycle. Each one commits the new state,
its ledger event and any outgoing command in a single transaction."""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from relay.application.assignments import (
    BUILD,
    GreenRun,
    complete,
    finish_green_run,
    owe,
    retire_all_of_behaviour,
    start_green_run,
)
from relay.application.closure import (
    CANCELLED,
    closure_of_behaviour,
    lock_gate,
    require_basis,
    story_basis,
)
from relay.application.owed import owe_contract_again, owe_red
from relay.application.reviews import open_cycle
from relay.application.stories import add_behaviour_to_story, require_story_at_work
from relay.domain.behaviour import Behaviour, BehaviourState, TransitionRefused
from relay.domain.evidence import (
    Execution,
    GreenVerdict,
    PairedRed,
    ProcessContract,
    RedRejection,
    RedVerdict,
    is_gate_bearing,
    refuse_green_before_launch,
    refuse_isolation,
)
from relay.domain.identifiers import require_identifier
from relay.domain.story import ActionRefused, Principal, PrincipalKind
from relay.git.checkout import (
    CandidateMoved,
    CheckoutFailed,
    attempt_head,
    attempt_ref,
    canonical_repository,
    pristine_checkout,
    trunk_head,
)
from relay.git.mirror import Mirror
from relay.persistence.database import Connection, Pool
from relay.persistence.ledger import append_event
from relay.testing.oracle import ProcessOracle
from relay.transport.envelope import Envelope, MessageKind

CONTROL_PLANE = "relay/control-plane"


_RELAY = Principal(PrincipalKind.CONTROL_PLANE, "relay/control-plane")


def _require_verifier(principal: Principal, to: str) -> None:
    """A behaviour, its test design and its contract are the Verifier's. A story's
    revision and a decision basis say that a caller read what is current; they do not
    say who it is."""
    if principal.kind is not PrincipalKind.WORKER or principal.role != "verifier":
        raise ActionRefused(f"{to} is for the verifier to do")


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
        """One project per managed trunk: what a candidate must still satisfy is recorded
        per project, and a project's lock covers only its own pushes. Projects may share
        a repository on different branches."""
        require_identifier("project", project_id)
        address = canonical_repository(repository)
        async with self._pool.connection() as connection, connection.transaction():
            managed = await (
                await connection.execute(
                    "INSERT INTO projects (project_id, repository, trunk) VALUES (%s, %s, %s) "
                    "ON CONFLICT (repository, trunk) DO NOTHING RETURNING project_id",
                    (require_identifier("project", project_id), address, trunk),
                )
            ).fetchone()
            if managed is None:
                raise ValueError(
                    f"branch {trunk!r} of that repository is already managed by another project"
                )
            await connection.execute(
                "INSERT INTO project_gates (project_id) VALUES (%s)", (project_id,)
            )
            await append_event(connection, project_id, "project.opened", payload={"trunk": trunk})

    async def propose(
        self,
        principal: Principal,
        project_id: str,
        story_id: str,
        behaviour_id: str,
        summary: str,
        demonstrates: Sequence[str],
        *,
        basis: int,
        story_revision: int,
    ) -> None:
        """Add a behaviour to a story that is being worked on, naming the acceptance
        criteria it will demonstrate. There is no behaviour without a story. The
        proposal names the story revision and decision basis its author read: a
        criterion keeps its name when it is reworded, and work must not start from the
        old wording."""
        _require_verifier(principal, "proposing a behaviour")
        behaviour = Behaviour.propose(behaviour_id, summary)
        # Read once, before anything is awaited: what is checked, stored and written to
        # the ledger is this, whatever the caller does to its own list meanwhile.
        demonstrates = tuple(demonstrates)
        correlation_id = uuid.uuid7()
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
            position = await add_behaviour_to_story(
                connection, project_id, story_id, demonstrates, basis, story_revision
            )
            await connection.execute(
                "INSERT INTO behaviours (project_id, behaviour_id, summary, state, "
                "correlation_id, story_id, position, demonstrates) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    project_id,
                    behaviour_id,
                    summary,
                    behaviour.state.value,
                    correlation_id,
                    story_id,
                    position,
                    list(demonstrates),
                ),
            )
            await append_event(
                connection,
                project_id,
                "behaviour.proposed",
                subject_id=behaviour_id,
                payload={
                    "summary": summary,
                    "story_id": story_id,
                    "demonstrates": list(demonstrates),
                },
                correlation_id=correlation_id,
            )

    async def begin_test_design(
        self, principal: Principal, project_id: str, behaviour_id: str
    ) -> None:
        _require_verifier(principal, "designing a behaviour's test")
        async with self._changing(project_id, behaviour_id, starts="design") as (
            connection,
            behaviour,
            correlation,
        ):
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
        principal: Principal,
        project_id: str,
        behaviour_id: str,
        contract: ProcessContract,
        *,
        basis: int,
    ) -> None:
        """The Verifier's contract, with the decision basis it was written on."""
        _require_verifier(principal, "registering an acceptance contract")
        async with self._changing(project_id, behaviour_id, starts="work") as (
            connection,
            behaviour,
            correlation,
        ):
            story_id = await _story_of(connection, project_id, behaviour_id)
            require_basis(basis, await story_basis(connection, project_id, story_id))
            behaviour.register_acceptance_test(contract)
            await connection.execute(
                "UPDATE behaviours SET current_on = %s WHERE project_id = %s AND behaviour_id = %s",
                (basis, project_id, behaviour_id),
            )
            await append_event(
                connection,
                project_id,
                "behaviour.acceptance_test_registered",
                subject_id=behaviour_id,
                payload={"contract": contract.to_dict(), "contract_digest": contract.digest},
                correlation_id=correlation,
            )
            # Saved before the red is owed: what is owed is read from the record.
            await save_behaviour(connection, project_id, behaviour)
            await owe_red(connection, project_id, behaviour_id)

    async def observe_owed_reds(self, project_id: str) -> None:
        """Carry out every red that is owed: the pass's to call. One that cannot be
        observed now (the repository does not answer, its scope is stopped) stays owed."""
        async with self._pool.connection() as connection:
            owed = await (
                await connection.execute(
                    "SELECT behaviour_id FROM assignments WHERE project_id = %s "
                    "AND kind = 'red' AND state = 'wanted' ORDER BY created_at",
                    (project_id,),
                )
            ).fetchall()
        for each in owed:
            try:
                await self.run_red(_RELAY, project_id, each["behaviour_id"])
            except CheckoutFailed, ActionRefused, TransitionRefused:
                continue

    async def run_red(self, principal: Principal, project_id: str, behaviour_id: str) -> RedVerdict:
        """Observe the registered contract on the project's own trunk and judge the result.

        This is the only way a behaviour reaches the Builder. The caller chooses neither the
        source nor the observation: Relay reads the trunk head itself, launches the subject
        itself, and records the run, the verdict and any release in one transaction. It is
        the control plane's to ask for: no worker starts the oracle.
        """
        if principal.kind is not PrincipalKind.CONTROL_PLANE:
            raise ActionRefused("observing a red is for the control plane to ask for")
        async with self._changing(project_id, behaviour_id, starts="evidence") as (
            connection,
            behaviour,
            correlation,
        ):
            contract = behaviour.require_awaiting_red()
            cycle = behaviour.red_cycle
            story_id = await _story_of(connection, project_id, behaviour_id)
            launched_on = await story_basis(connection, project_id, story_id)
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
            closed_by = await closure_of_behaviour(connection, project_id, behaviour_id)
            # A run that outlives a cancel, or a decision of the Product Owner's, is
            # kept as what was seen and changes nothing.
            overtaken = closed_by == CANCELLED or launched_on != await story_basis(
                connection, project_id, story_id
            )
            verdict = behaviour.record_red_run(
                execution, started_in_cycle=cycle, withdrawn=overtaken
            )
            execution_id = await record_execution(
                connection, project_id, behaviour_id, "red", contract.digest, execution, launched_on
            )
            evidence = {
                "execution_id": str(execution_id),
                "revision": execution.revision,
                "isolation": execution.isolation,
                "runtime_identity": execution.runtime_identity,
            }
            if verdict.rejection is not RedRejection.SUPERSEDED:
                # The red that was owed has been observed, whichever way it went. One
                # that was overtaken in flight observed nothing of what is owed now.
                await connection.execute(
                    "UPDATE assignments SET state = 'done', grant_id = %s, granted_to = %s, "
                    "first_effect_at = now() WHERE project_id = %s AND behaviour_id = %s "
                    "AND kind = 'red' AND state = 'wanted'",
                    (uuid.uuid7(), CONTROL_PLANE, project_id, behaviour_id),
                )
            if not verdict.is_valid:
                await self._red_rejected(
                    connection, project_id, behaviour_id, correlation, verdict, contract, evidence
                )
                if verdict.rejection is not RedRejection.SUPERSEDED and (
                    behaviour.state is BehaviourState.VALIDATING_RED
                ):
                    # Running it again would show the same. The contract is the
                    # Verifier's to put right, briefed with what was observed.
                    await owe_contract_again(connection, project_id, behaviour_id, verdict.reason)
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
            await owe(
                connection,
                BUILD,
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
                issued_on={"red_execution_id": str(execution_id)},
            )
            return verdict

    async def run_green(
        self,
        project_id: str,
        behaviour_id: str,
        attempt: int,
        grant: uuid.UUID,
        *,
        expecting: str | None = None,
        still_heard: Callable[[Connection], Awaitable[bool]] | None = None,
    ) -> GreenVerdict:
        """Observe the contract on the candidate a Builder published for this attempt.

        The caller names an attempt and the grant it was given for the build, nothing
        more. Relay reads the candidate from the project's own repository, checks it was
        built on the revision the red was observed on, launches it itself and pairs the
        result with that red. That this run began is committed before anything is
        launched, and the run answers that one assignment: if the work is replaced or
        withdrawn while the run is in flight, what it observed is kept and changes nothing.

        `expecting` is the commit the attempt was published as: if the attempt ref names
        another by now, nothing is observed. `still_heard` is asked in the transaction
        that would accept the verdict: a caller that has been taken over meanwhile has
        its run kept as evidence, and decides nothing.
        """
        attempt_ref(project_id, behaviour_id, attempt)  # refuse a malformed attempt first
        async with self._changing(project_id, behaviour_id, starts="evidence") as (
            connection,
            behaviour,
            _,
        ):
            contract = behaviour.require_awaiting_green()
            story_id = await _story_of(connection, project_id, behaviour_id)
            run = await start_green_run(
                connection,
                grant,
                project_id=project_id,
                behaviour_id=behaviour_id,
                attempt=attempt,
                basis=await story_basis(connection, project_id, story_id),
            )
            isolation = self._oracle.isolation
            repository, _ = await source_of(connection, project_id)
            red = await _paired_red(connection, project_id, behaviour_id)

        if refusal := refuse_green_before_launch(contract, isolation.name, red):
            await self._green_refused(project_id, behaviour_id, attempt, contract, refusal, run)
            return refusal
        assert red is not None
        try:
            candidate = await attempt_head(repository, project_id, behaviour_id, attempt)
            if expecting is not None and candidate != expecting:
                raise CandidateMoved("the attempt no longer names the candidate that was kept")
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
            await self._preparation_failed(
                "green", project_id, behaviour_id, contract, failure, run
            )
            raise
        return await self._record_green(
            project_id,
            behaviour_id,
            attempt,
            execution,
            red,
            run,
            contract,
            descends=descends,
            still_heard=still_heard,
        )

    async def reopen_red(
        self, principal: Principal, project_id: str, behaviour_id: str, reason: str
    ) -> None:
        """Send a behaviour back for a fresh red and drop the old pairing, on the record.
        The Verifier's to do: it owns the contract the red is a red of."""
        if principal.kind is not PrincipalKind.WORKER or principal.role != "verifier":
            raise ActionRefused("reopening a red is for the verifier to do")
        if not reason.strip():
            raise ValueError("reopening a red needs a stated reason")
        async with self._changing(project_id, behaviour_id, starts="evidence") as (
            connection,
            behaviour,
            correlation,
        ):
            behaviour.reopen_red()
            await retire_all_of_behaviour(connection, project_id, behaviour_id)
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
            await save_behaviour(connection, project_id, behaviour)
            await owe_red(connection, project_id, behaviour_id)

    async def _green_refused(
        self,
        project_id: str,
        behaviour_id: str,
        attempt: int,
        contract: ProcessContract,
        refusal: GreenVerdict,
        run: GreenRun,
    ) -> None:
        """Nothing was launched and no execution is claimed; the refusal is still recorded."""
        assert refusal.rejection is not None
        async with self._changing(project_id, behaviour_id) as (connection, _, correlation):
            await finish_green_run(connection, run)
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
        run: GreenRun,
        observed: ProcessContract,
        *,
        descends: bool,
        still_heard: Callable[[Connection], Awaitable[bool]] | None = None,
    ) -> GreenVerdict:
        """`observed` is the contract the run was launched to observe. The record says
        that one, whatever the behaviour's contract has become meanwhile."""
        async with self._changing(project_id, behaviour_id) as (connection, behaviour, correlation):
            verdict = behaviour.record_green_run(
                execution,
                red,
                await _paired_red(connection, project_id, behaviour_id),
                candidate_descends_from_red=descends,
                still_owed=await finish_green_run(connection, run)
                and (still_heard is None or await still_heard(connection)),
                basis_moved=run.basis
                != await story_basis(
                    connection, project_id, await _story_of(connection, project_id, behaviour_id)
                ),
                observed=observed,
            )
            contract = observed
            execution_id = await record_execution(
                connection, project_id, behaviour_id, "green", contract.digest, execution, run.basis
            )
            evidence: dict[str, Any] = {
                "attempt": attempt,
                # Whether this is a judgement of the candidate, or only what a run that
                # was overtaken in flight happened to see. Whoever takes the work up
                # again replays the first and never the second.
                "judged": verdict.judges_the_candidate,
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
                await complete(connection, run.assignment_id)
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
        run: GreenRun | None = None,
    ) -> None:
        """Nothing ran, and the ledger says so: a failed checkout is not a silent non-event."""
        async with self._changing(project_id, behaviour_id) as (connection, _, correlation):
            if run is not None:
                await finish_green_run(connection, run)
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
        self, project_id: str, behaviour_id: str, starts: str | None = None
    ) -> AsyncIterator[tuple[Connection, Behaviour, uuid.UUID]]:
        """Lock the behaviour, let the caller change it, and save it in the same transaction.

        `starts` marks an operation that begins something new on the behaviour ("design"
        for entering test design, "work" for anything after): it is refused unless the
        behaviour's story is being worked on. Recording what an operation already in
        flight observed is never refused."""
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
            row = await (
                await connection.execute(
                    "SELECT * FROM behaviours WHERE project_id = %s AND behaviour_id = %s "
                    "FOR UPDATE",
                    (project_id, behaviour_id),
                )
            ).fetchone()
            if row is None:
                raise LookupError(f"unknown behaviour {behaviour_id!r} in project {project_id!r}")
            if starts is not None:
                await require_story_at_work(
                    connection,
                    project_id,
                    row,
                    entering_design=starts == "design",
                    on_its_contract=starts == "evidence",
                )
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


async def _story_of(connection: Connection, project_id: str, behaviour_id: str) -> str:
    row = await (
        await connection.execute(
            "SELECT story_id FROM behaviours WHERE project_id = %s AND behaviour_id = %s",
            (project_id, behaviour_id),
        )
    ).fetchone()
    assert row is not None
    story_id: str = row["story_id"]
    return story_id


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
    behaviour_id: str | None,
    purpose: str,
    contract_digest: str,
    execution: Execution,
    basis: int | None = None,
) -> uuid.UUID:
    execution_id = uuid.uuid7()
    observation = execution.observation
    await connection.execute(
        "INSERT INTO executions (execution_id, project_id, behaviour_id, purpose, revision, "
        "contract_digest, isolation, runtime_identity, exit_code, stdout, stderr, ineligibility, "
        "basis) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
            basis,
        ),
    )
    return execution_id


async def specify_in(
    connection: Connection,
    principal: Principal,
    project_id: str,
    story_id: str,
    behaviour_id: str,
    summary: str,
    demonstrates: Sequence[str],
    contract: ProcessContract,
    *,
    basis: int,
    story_revision: int,
) -> None:
    """A behaviour proposed, taken into test design and given its contract, in the
    caller's transaction: there is no state in which a behaviour that was specified
    exists without its contract. Its red is owed with it."""
    _require_verifier(principal, "specifying a behaviour")
    behaviour = Behaviour.propose(behaviour_id, summary)
    demonstrates = tuple(demonstrates)
    correlation_id = uuid.uuid7()
    await lock_gate(connection, project_id)
    position = await add_behaviour_to_story(
        connection, project_id, story_id, demonstrates, basis, story_revision
    )
    await connection.execute(
        "INSERT INTO behaviours (project_id, behaviour_id, summary, state, "
        "correlation_id, story_id, position, demonstrates) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            project_id,
            behaviour_id,
            summary,
            behaviour.state.value,
            correlation_id,
            story_id,
            position,
            list(demonstrates),
        ),
    )
    row = await (
        await connection.execute(
            "SELECT * FROM behaviours WHERE project_id = %s AND behaviour_id = %s FOR UPDATE",
            (project_id, behaviour_id),
        )
    ).fetchone()
    assert row is not None
    await require_story_at_work(connection, project_id, row, entering_design=True)
    behaviour.begin_test_design()
    behaviour.register_acceptance_test(contract)
    await connection.execute(
        "UPDATE behaviours SET current_on = %s WHERE project_id = %s AND behaviour_id = %s",
        (basis, project_id, behaviour_id),
    )
    await save_behaviour(connection, project_id, behaviour)
    for event_type, payload in (
        (
            "behaviour.proposed",
            {"summary": summary, "story_id": story_id, "demonstrates": list(demonstrates)},
        ),
        ("behaviour.test_design_started", {}),
        (
            "behaviour.acceptance_test_registered",
            {"contract": contract.to_dict(), "contract_digest": contract.digest},
        ),
    ):
        await append_event(
            connection,
            project_id,
            event_type,
            subject_id=behaviour_id,
            payload=payload,
            correlation_id=correlation_id,
        )
    await owe_red(connection, project_id, behaviour_id)


async def rewrite_contract_in(
    connection: Connection,
    principal: Principal,
    project_id: str,
    behaviour_id: str,
    contract: ProcessContract,
    *,
    basis: int,
) -> None:
    """The Verifier replaces a contract whose red showed it cannot serve, in the
    caller's transaction. The old red cycle is over, and a red of the new one is owed."""
    _require_verifier(principal, "rewriting an acceptance contract")
    await lock_gate(connection, project_id)
    row = await (
        await connection.execute(
            "SELECT * FROM behaviours WHERE project_id = %s AND behaviour_id = %s FOR UPDATE",
            (project_id, behaviour_id),
        )
    ).fetchone()
    if row is None:
        raise LookupError(f"unknown behaviour {behaviour_id!r} in project {project_id!r}")
    await require_story_at_work(connection, project_id, row, entering_design=False)
    require_basis(basis, await story_basis(connection, project_id, row["story_id"]))
    behaviour = load_behaviour(row)
    if behaviour.state is not BehaviourState.VALIDATING_RED:
        raise ActionRefused(f"{behaviour_id} is not waiting for a red: its contract stands")
    behaviour.amend_contract(contract)
    await connection.execute(
        "UPDATE behaviours SET current_on = %s WHERE project_id = %s AND behaviour_id = %s",
        (basis, project_id, behaviour_id),
    )
    await save_behaviour(connection, project_id, behaviour)
    await connection.execute(
        "UPDATE assignments SET state = 'retired', grant_id = NULL, granted_to = NULL "
        "WHERE project_id = %s AND behaviour_id = %s AND kind = 'red' AND state = 'wanted'",
        (project_id, behaviour_id),
    )
    await append_event(
        connection,
        project_id,
        "behaviour.acceptance_test_rewritten",
        subject_id=behaviour_id,
        payload={"contract": contract.to_dict(), "contract_digest": contract.digest},
        correlation_id=row["correlation_id"],
    )
    await owe_red(connection, project_id, behaviour_id)
