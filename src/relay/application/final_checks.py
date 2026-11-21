"""A feature's final checks. Everything the project's trunk must still satisfy is
observed on the commit that is trunk when the checks start, and the feature is completed
only if that commit is still trunk, and everything else the batch recorded is still so,
when they have all passed.

Like an integration, a batch records what it is a check of, runs outside any
transaction, and is allowed to decide only what it recorded. Unlike anything else in
Relay, the transaction that ends a batch reads the repository while it holds the
project's gate lock exclusively, with a deadline: the summary certifies one commit, and
nothing of Relay's may move trunk between that reading and the commit of the summary.
"""

from __future__ import annotations

import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from relay.application.assignments import take_up
from relay.application.behaviours import record_execution
from relay.application.closure import feature_basis, lock_gate
from relay.application.completion import (
    CONTROL_PLANE,
    owe_completion,
    roadmap_in_force,
    stopped,
    stories_of,
)
from relay.application.stories import _digest as digest_of
from relay.application.stories import _open_gate as open_gate
from relay.domain.evidence import CheckContract, is_gate_bearing
from relay.domain.story import ActionRefused
from relay.git.checkout import CheckoutFailed, pristine_checkout, trunk_head
from relay.persistence.database import Connection, Pool
from relay.persistence.ledger import append_event
from relay.persistence.outbox import enqueue
from relay.testing.oracle import ProcessOracle
from relay.transport.envelope import Envelope, MessageKind

_SUCCESSORS_TAKEN_UP_AT_ONCE = 3
_ROUNDS_OF_TAKING_UP = 3


@dataclass(frozen=True)
class CompletionReceipt:
    """What became of one generation of a feature's final checks."""

    outcome: str
    batch_id: uuid.UUID | None = None
    reason: str = ""


class Completion:
    def __init__(self, pool: Pool, oracle: ProcessOracle, remote_timeout: float = 120.0) -> None:
        self._pool = pool
        self._oracle = oracle
        self._remote_timeout = remote_timeout

    async def owed(self, project_id: str) -> list[tuple[uuid.UUID, str]]:
        """The completions wanted for a project, each with the request it is for."""
        async with self._pool.connection() as connection:
            rows = await (
                await connection.execute(
                    "SELECT assignment_id, issued_on->>'request_id' AS request_id FROM assignments "
                    "WHERE project_id = %s AND kind = 'complete' AND state = 'wanted' "
                    "ORDER BY created_at",
                    (project_id,),
                )
            ).fetchall()
        return [(row["assignment_id"], row["request_id"]) for row in rows]

    async def summary_of(self, project_id: str, feature_id: str) -> dict[str, Any] | None:
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT summary FROM feature_summaries WHERE project_id = %s "
                    "AND feature_id = %s",
                    (project_id, feature_id),
                )
            ).fetchone()
        return row["summary"] if row else None

    async def take_up(self, assignment_id: uuid.UUID) -> CompletionReceipt:
        """Start the next generation of the request this completion is owed for, run its
        checks, and end it. A generation that finds what it recorded has changed owes a
        successor, and that one is taken up here too, a bounded number of times: nobody
        else is told that it now exists."""
        receipt, successor = await self._take_up(assignment_id)
        for _ in range(_SUCCESSORS_TAKEN_UP_AT_ONCE):
            if successor is None:
                break
            _, successor = await self._take_up(successor)
        # What became of the generation bound to this completion: asked again, the
        # record gives the same answer.
        return receipt

    async def take_up_what_is_owed(self, project_id: str) -> None:
        """Take up the completions owed for a project. A request is attempted once in a
        pass, however many completions it has been owed: one that keeps being put off
        waits for the next pass instead of holding this one."""
        attempted: set[str] = set()
        for _ in range(_ROUNDS_OF_TAKING_UP):
            owed = [
                (assignment, request)
                for assignment, request in await self.owed(project_id)
                if request not in attempted
            ]
            if not owed:
                break
            for assignment, request in owed:
                attempted.add(request)
                try:
                    await self.take_up(assignment)
                except CheckoutFailed, ActionRefused:
                    # The repository could not be read, or this process cannot observe
                    # checks. The completion stays owed.
                    continue

    async def _take_up(
        self, assignment_id: uuid.UUID
    ) -> tuple[CompletionReceipt, uuid.UUID | None]:
        async with self._pool.connection() as connection:
            owed = await (
                await connection.execute(
                    "SELECT a.project_id, a.feature_id, a.state, p.repository, p.trunk "
                    "FROM assignments a JOIN projects p USING (project_id) "
                    "WHERE a.assignment_id = %s AND a.kind = 'complete'",
                    (assignment_id,),
                )
            ).fetchone()
            if owed is None:
                raise LookupError(f"no completion is owed under {assignment_id}")
            if owed["state"] != "wanted":
                # Before the repository is asked anything: what became of a completion
                # that was already taken up, or retired unused, is on record.
                return await _disposition(connection, assignment_id), None
            waits = await stopped(connection, owed["project_id"], owed["feature_id"])
        if waits is not None:
            return _not_started(waits), None
        if not is_gate_bearing(self._oracle.isolation.name):
            raise ActionRefused(
                f"{self._oracle.isolation.name} execution cannot complete a feature; "
                "checks must be observed in a container"
            )
        head = await trunk_head(owed["repository"], owed["trunk"], timeout=self._remote_timeout)
        batch = await self._start(assignment_id, owed["project_id"], owed["feature_id"], head)
        if isinstance(batch, CompletionReceipt):
            return batch, None
        try:
            await self._run_checks(batch, owed["repository"])
            return await self._end(batch["batch_id"])
        except BaseException:
            # The checks, or the transaction that would have ended them, were cut short.
            # Nothing was decided if the batch is still running, and nobody else will
            # end it while this process lives.
            await self._cut_short(batch)
            raise

    async def _cut_short(self, batch: dict[str, Any]) -> None:
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, batch["project_id"])
            await _locked_feature(connection, batch["project_id"], batch["feature_id"])
            running = await (
                await connection.execute(
                    "SELECT * FROM completion_batches WHERE batch_id = %s "
                    "AND state = 'running' FOR UPDATE",
                    (batch["batch_id"],),
                )
            ).fetchone()
            if running is not None:
                await _left_unfinished(connection, running, "the checks could not all be run")

    async def _start(
        self, assignment_id: uuid.UUID, project_id: str, feature_id: str, head: str
    ) -> dict[str, Any] | CompletionReceipt:
        """Under the gate lock, look again at everything that was looked at before the
        repository was read, and bind one generation to exactly this assignment."""
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
            await _locked_feature(connection, project_id, feature_id)
            assignment = await (
                await connection.execute(
                    "SELECT state, issued_on->>'request_id' AS request_id FROM assignments "
                    "WHERE assignment_id = %s FOR UPDATE",
                    (assignment_id,),
                )
            ).fetchone()
            assert assignment is not None
            if assignment["state"] != "wanted":
                return await _disposition(connection, assignment_id)
            waits = await stopped(connection, project_id, feature_id)
            if waits is not None:
                return _not_started(waits)
            request_id = uuid.UUID(assignment["request_id"])
            snapshot = await _snapshot(connection, project_id, feature_id)
            generation = await (
                await connection.execute(
                    "SELECT coalesce(max(generation), 0) + 1 AS next FROM completion_batches "
                    "WHERE request_id = %s",
                    (request_id,),
                )
            ).fetchone()
            assert generation is not None
            batch = {
                "batch_id": uuid.uuid7(),
                "request_id": request_id,
                "project_id": project_id,
                "feature_id": feature_id,
                "trunk": head,
                "snapshot": snapshot,
            }
            await connection.execute(
                "INSERT INTO completion_batches (batch_id, request_id, project_id, feature_id, "
                "generation, assignment_id, trunk, snapshot) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    batch["batch_id"],
                    request_id,
                    project_id,
                    feature_id,
                    generation["next"],
                    assignment_id,
                    head,
                    Jsonb(snapshot),
                ),
            )
            await take_up(connection, assignment_id, CONTROL_PLANE)
            return batch

    async def _run_checks(self, batch: dict[str, Any], repository: str) -> None:
        """Observe each obligation on the batch's commit, and commit each receipt as its
        check finishes: what was seen is on record whatever becomes of the batch."""
        for obligation in batch["snapshot"]["obligations"]:
            async with self._pool.connection() as connection:
                row = await (
                    await connection.execute(
                        "SELECT contract FROM obligations WHERE obligation_id = %s",
                        (obligation["obligation_id"],),
                    )
                ).fetchone()
            assert row is not None
            check = CheckContract.from_dict(row["contract"])
            # A tree of its own for every check: what one run leaves behind must not be
            # what the next one observes.
            with tempfile.TemporaryDirectory(prefix="relay-final-") as scratch:
                workdir = await pristine_checkout(
                    repository, batch["trunk"], Path(scratch) / "subject"
                )
                execution = await self._oracle.execute(batch["trunk"], workdir, check.launch)
            async with self._pool.connection() as connection, connection.transaction():
                execution_id = await record_execution(
                    connection,
                    batch["project_id"],
                    None,
                    "final_check",
                    check.digest,
                    execution,
                    batch["snapshot"]["basis"],
                )
                await connection.execute(
                    "INSERT INTO completion_receipts (batch_id, obligation_id, digest, revision, "
                    "execution_id, satisfied) VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        batch["batch_id"],
                        obligation["obligation_id"],
                        check.digest,
                        execution.revision,
                        execution_id,
                        check.is_satisfied_by(execution),
                    ),
                )

    async def _end(self, batch_id: uuid.UUID) -> tuple[CompletionReceipt, uuid.UUID | None]:
        """The rule that ends a generation whose checks have all run. Returns what became
        of it and, if what it recorded was found changed, the completion owed in its
        place."""
        async with self._pool.connection() as connection:
            located = await (
                await connection.execute(
                    "SELECT b.project_id, b.feature_id, p.repository, p.trunk "
                    "FROM completion_batches b JOIN projects p USING (project_id) "
                    "WHERE b.batch_id = %s",
                    (batch_id,),
                )
            ).fetchone()
        assert located is not None
        project_id, feature_id = located["project_id"], located["feature_id"]
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id, exclusive=True)
            await _locked_feature(connection, project_id, feature_id)
            batch = await (
                await connection.execute(
                    "SELECT * FROM completion_batches WHERE batch_id = %s FOR UPDATE", (batch_id,)
                )
            ).fetchone()
            assert batch is not None
            if batch["state"] != "running":
                # Its request ended, or another call ended it. A request that ends takes
                # its running generation with it, so a running one is always current.
                return CompletionReceipt(batch["state"], batch_id, batch["reason"] or ""), None
            waits = await stopped(connection, project_id, feature_id)
            if waits is not None:
                await _owed_again(connection, batch, "held", f"the feature is {waits}")
                return CompletionReceipt("held", batch_id, f"the feature is {waits}"), None
            receipts = await (
                await connection.execute(
                    "SELECT r.obligation_id::text, r.digest, r.satisfied, "
                    "r.execution_id::text, o.name, o.source, o.behaviour_id "
                    "FROM completion_receipts r JOIN obligations o USING (obligation_id) "
                    "WHERE r.batch_id = %s ORDER BY o.created_at",
                    (batch_id,),
                )
            ).fetchall()
            # This is reached only after every check has run and its receipt committed.
            assert [
                {"obligation_id": each["obligation_id"], "digest": each["digest"]}
                for each in receipts
            ] == batch["snapshot"]["obligations"]
            if batch["snapshot"] != await _snapshot(connection, project_id, feature_id):
                why = "what these checks were a check of changed while they were running"
                successor = await _owed_again(connection, batch, "discarded", why)
                return CompletionReceipt("discarded", batch_id, why), successor
            pushing = await (
                await connection.execute(
                    "SELECT 1 FROM integration_intents WHERE project_id = %s "
                    "AND state = 'outstanding'",
                    (project_id,),
                )
            ).fetchone()
            if pushing is not None:
                # A push may be on its way, or may have landed with its settlement not
                # yet committed. Either way the records and the repository cannot both
                # be read as one moment. Its settlement takes this up again.
                why = "an integration of this project is on its way to trunk"
                await _owed_again(connection, batch, "deferred", why)
                return CompletionReceipt("deferred", batch_id, why), None
            try:
                # Under the exclusive gate lock, and with no push outstanding: nothing
                # of Relay's can move this trunk before the summary commits.
                head = await trunk_head(
                    located["repository"], located["trunk"], timeout=self._remote_timeout
                )
            except CheckoutFailed:
                why = "trunk could not be read"
                await _owed_again(connection, batch, "deferred", why)
                return CompletionReceipt("deferred", batch_id, why), None
            if head != batch["trunk"]:
                why = "trunk moved while the checks were running"
                successor = await _owed_again(connection, batch, "discarded", why)
                return CompletionReceipt("discarded", batch_id, why), successor
            if not all(each["satisfied"] for each in receipts):
                return await _fail(connection, batch, receipts), None
            return await _complete(connection, batch, receipts), None


async def end_batches_left_running(connection: Connection, project_id: str) -> None:
    """On start: whatever was running these checks is gone. Each is ended, with one
    completion owed after it; its receipts stay as what was seen. Under the gate lock."""
    left = await (
        await connection.execute(
            "SELECT * FROM completion_batches WHERE project_id = %s AND state = 'running' "
            "ORDER BY created_at FOR UPDATE",
            (project_id,),
        )
    ).fetchall()
    for batch in left:
        await _left_unfinished(connection, batch, "the process running these checks ended")


async def _left_unfinished(connection: Connection, batch: dict[str, Any], why: str) -> None:
    """A running generation that will not be ended by its own checks: it decided
    nothing, and one completion is owed after it."""
    waits = await stopped(connection, batch["project_id"], batch["feature_id"])
    if waits is not None:
        await _owed_again(connection, batch, "held", f"the feature is {waits}")
    else:
        await _owed_again(connection, batch, "interrupted", why)


async def _owed_again(
    connection: Connection, batch: dict[str, Any], state: str, reason: str
) -> uuid.UUID:
    """This generation decided nothing; one new completion is owed for its request. This
    is the only place a completion is owed again."""
    await _close(connection, batch, state, reason)
    return await owe_completion(
        connection, batch["project_id"], batch["feature_id"], batch["request_id"]
    )


async def _disposition(connection: Connection, assignment_id: uuid.UUID) -> CompletionReceipt:
    """What became of a completion that is no longer wanted."""
    batch = await (
        await connection.execute(
            "SELECT batch_id, state, reason FROM completion_batches WHERE assignment_id = %s",
            (assignment_id,),
        )
    ).fetchone()
    if batch is None:
        return CompletionReceipt("retired", None, "this completion was withdrawn unused")
    return CompletionReceipt(batch["state"], batch["batch_id"], batch["reason"] or "")


def _not_started(waits: str) -> CompletionReceipt:
    return CompletionReceipt("not_started", None, f"the feature is {waits}: nothing starts")


async def _snapshot(connection: Connection, project_id: str, feature_id: str) -> dict[str, Any]:
    """Everything a batch is a check of, apart from the commit."""
    roadmap = await roadmap_in_force(connection, project_id, feature_id)
    assert roadmap is not None
    obligations = await (
        await connection.execute(
            "SELECT obligation_id::text, digest FROM obligations WHERE project_id = %s "
            "AND state = 'active' ORDER BY created_at",
            (project_id,),
        )
    ).fetchall()
    return {
        "roadmap_revision": roadmap["revision"],
        "stories": [
            {"story_id": each["story_id"], "state": each["state"], "revision": each["revision"]}
            for each in await stories_of(connection, project_id, feature_id, roadmap["revision"])
        ],
        "obligations": obligations,
        "basis": await feature_basis(connection, project_id, feature_id),
    }


async def _complete(
    connection: Connection, batch: dict[str, Any], receipts: list[dict[str, Any]]
) -> CompletionReceipt:
    """Every check passed on the commit that is trunk. The feature is completed, and its
    summary and the notice of it are written here, once."""
    project_id, feature_id = batch["project_id"], batch["feature_id"]
    await _close(connection, batch, "completed", None)
    await connection.execute(
        "UPDATE completion_requests SET state = 'completed' WHERE request_id = %s",
        (batch["request_id"],),
    )
    await connection.execute(
        "UPDATE features SET state = 'completed' WHERE project_id = %s AND feature_id = %s",
        (project_id, feature_id),
    )
    summary = await _summary(connection, batch, receipts)
    await connection.execute(
        "INSERT INTO feature_summaries (project_id, feature_id, batch_id, summary) "
        "VALUES (%s, %s, %s, %s)",
        (project_id, feature_id, batch["batch_id"], Jsonb(summary)),
    )
    completed = await append_event(
        connection,
        project_id,
        "feature.completed",
        subject_id=feature_id,
        payload={"batch_id": str(batch["batch_id"]), "trunk": batch["trunk"]},
    )
    await enqueue(
        connection,
        f"relay:{project_id}:inbox:product-owner",
        Envelope(
            message_kind=MessageKind.EVENT,
            event_type="feature.completed",
            project_id=project_id,
            sender=CONTROL_PLANE,
            recipient="product-owner",
            idempotency_key=f"completed:{project_id}:{feature_id}",
            correlation_id=uuid.uuid7(),
            causation_id=completed.event_id,
            payload={"feature_id": feature_id, "trunk": batch["trunk"]},
        ),
    )
    return CompletionReceipt("completed", batch["batch_id"])


async def _fail(
    connection: Connection, batch: dict[str, Any], receipts: list[dict[str, Any]]
) -> CompletionReceipt:
    """A check is not satisfied on the commit that is trunk. Nothing is completed: a
    person is shown what failed and says whether to check again or change the roadmap."""
    project_id, feature_id = batch["project_id"], batch["feature_id"]
    reason = "not satisfied on trunk: " + ", ".join(
        each["name"] for each in receipts if not each["satisfied"]
    )
    await _close(connection, batch, "failed", reason)
    await connection.execute(
        "UPDATE completion_requests SET state = 'failed' WHERE request_id = %s",
        (batch["request_id"],),
    )
    await connection.execute(
        "UPDATE features SET state = 'needs_decision' WHERE project_id = %s AND feature_id = %s",
        (project_id, feature_id),
    )
    manifest = {
        "batch_id": str(batch["batch_id"]),
        "trunk": batch["trunk"],
        "checked": _as_checked(receipts),
    }
    await open_gate(
        connection,
        project_id,
        feature_id,
        kind="completion",
        story_id=None,
        basis={
            "batch_id": str(batch["batch_id"]),
            "evidence": digest_of(manifest),
            "roadmap_revision": batch["snapshot"]["roadmap_revision"],
            "decisions": batch["snapshot"]["basis"],
        },
        manifest=manifest,
    )
    return CompletionReceipt("failed", batch["batch_id"], reason)


def _as_checked(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each obligation that was checked, by identity, with the run that observed it."""
    return [
        {
            "obligation_id": each["obligation_id"],
            "name": each["name"],
            "source": each["source"],
            "behaviour_id": each["behaviour_id"],
            "digest": each["digest"],
            "execution_id": each["execution_id"],
            "satisfied": each["satisfied"],
        }
        for each in receipts
    ]


async def _summary(
    connection: Connection, batch: dict[str, Any], receipts: list[dict[str, Any]]
) -> dict[str, Any]:
    """What was delivered and checked, from records and nothing else. It does not say
    the feature is valuable: that is not something Relay can observe."""
    project_id, feature_id = batch["project_id"], batch["feature_id"]
    feature = await (
        await connection.execute(
            "SELECT problem FROM features WHERE project_id = %s AND feature_id = %s",
            (project_id, feature_id),
        )
    ).fetchone()
    assert feature is not None
    stories = await (
        await connection.execute(
            # Corrected by a story of the roadmap that was certified: a correction that
            # was proposed and then dropped corrected nothing.
            "SELECT s.story_id, s.title, s.state, s.revision, (SELECT c.story_id "
            "FROM stories c JOIN roadmap_entries e USING (project_id, story_id) "
            "WHERE c.project_id = s.project_id AND c.corrects = s.story_id "
            "AND e.feature_id = s.feature_id AND e.revision = %s AND e.kind <> 'dropped') "
            "AS corrected_by, g.gate_id::text, g.manifest_digest FROM stories s "
            "LEFT JOIN gates g ON g.project_id = s.project_id AND g.story_id = s.story_id "
            "AND g.kind = 'story' AND g.state = 'decided' AND g.outcome = 'approved' "
            "WHERE s.project_id = %s AND s.feature_id = %s ORDER BY s.roadmap_revision, s.position",
            (batch["snapshot"]["roadmap_revision"], project_id, feature_id),
        )
    ).fetchall()
    behaviours = await (
        await connection.execute(
            "SELECT b.behaviour_id, b.story_id, b.summary, b.superseded_by FROM behaviours b "
            "JOIN stories s USING (project_id, story_id) WHERE b.project_id = %s "
            "AND s.feature_id = %s AND b.state IN ('integrated', 'completed') "
            "ORDER BY s.position, b.position",
            (project_id, feature_id),
        )
    ).fetchall()
    earlier = await (
        await connection.execute(
            "SELECT revision FROM roadmaps WHERE project_id = %s AND feature_id = %s "
            "AND state = 'superseded' ORDER BY revision",
            (project_id, feature_id),
        )
    ).fetchall()
    decisions = await (
        await connection.execute(
            "SELECT d.number, d.source, d.text, nullif(d.scope_story, '') AS story_id "
            "FROM decisions d WHERE d.project_id = %s AND d.feature_id = %s AND NOT EXISTS "
            "(SELECT 1 FROM decisions n WHERE n.replaces = d.decision_id) ORDER BY d.number",
            (project_id, feature_id),
        )
    ).fetchall()
    return {
        "feature_id": feature_id,
        "problem": feature["problem"],
        "roadmap_revision": batch["snapshot"]["roadmap_revision"],
        # Revisions that were in force before it. One that was proposed and never
        # approved was never the roadmap.
        "earlier_revisions": [each["revision"] for each in earlier],
        # Those in force, the Product Owner's and a model's told apart.
        "decisions": decisions,
        # What a model assumed and no person replaced: delivered on a guess.
        "assumptions": [each for each in decisions if each["source"] == "model"],
        "limitations": [
            limitation
            for behaviour in behaviours
            if behaviour["superseded_by"] is None
            for limitation in await _left_open(connection, project_id, behaviour["behaviour_id"])
        ],
        "stories": [
            {
                "story_id": story["story_id"],
                "title": story["title"],
                "outcome": "accepted" if story["state"] == "approved" else story["state"],
                "revision": story["revision"],
                "corrected_by": story["corrected_by"],
                "gate_id": story["gate_id"],
                "manifest_digest": story["manifest_digest"],
            }
            for story in stories
        ],
        "behaviours": [
            {**behaviour, "in_force": behaviour["superseded_by"] is None}
            for behaviour in behaviours
        ],
        "trunk": batch["trunk"],
        "checked": _as_checked(receipts),
        # Not measured yet, and never shown as zero.
        "usage": "unknown",
    }


async def _left_open(
    connection: Connection, project_id: str, behaviour_id: str
) -> list[dict[str, Any]]:
    """The findings a delivered behaviour still carries: from the review cycle that was
    integrated, under the policy frozen for that cycle, those that were neither withdrawn
    nor verified fixed there, and did not block. A finding's status alone does not say
    this: one whose fix was verified on the delivered candidate can still read open."""
    cycle = await (
        await connection.execute(
            "SELECT i.cycle_id, c.blocking_from FROM integration_intents i "
            "JOIN review_cycles c USING (cycle_id) WHERE i.project_id = %s "
            "AND i.behaviour_id = %s AND i.state = 'integrated' "
            "ORDER BY i.created_at DESC LIMIT 1",
            (project_id, behaviour_id),
        )
    ).fetchone()
    if cycle is None:
        return []
    return await (
        await connection.execute(
            "SELECT f.behaviour_id, f.finding_id::text, f.role, f.severity, f.claim "
            "FROM findings f WHERE f.project_id = %s AND f.behaviour_id = %s "
            "AND f.status <> 'withdrawn' AND f.severity < %s AND NOT EXISTS "
            "(SELECT 1 FROM finding_verifications v WHERE v.finding_id = f.finding_id "
            "AND v.cycle_id = %s) ORDER BY f.finding_id",
            (project_id, behaviour_id, cycle["blocking_from"], cycle["cycle_id"]),
        )
    ).fetchall()


async def _close(
    connection: Connection, batch: dict[str, Any], state: str, reason: str | None
) -> None:
    await connection.execute(
        "UPDATE completion_batches SET state = %s, reason = %s WHERE batch_id = %s",
        (state, reason, batch["batch_id"]),
    )
    await append_event(
        connection,
        batch["project_id"],
        f"completion.{state}",
        subject_id=batch["feature_id"],
        payload={"batch_id": str(batch["batch_id"]), "reason": reason},
    )


async def _locked_feature(connection: Connection, project_id: str, feature_id: str) -> None:
    await connection.execute(
        "SELECT 1 FROM features WHERE project_id = %s AND feature_id = %s FOR UPDATE",
        (project_id, feature_id),
    )
