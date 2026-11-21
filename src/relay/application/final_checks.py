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
    stories_of,
)
from relay.domain.evidence import CheckContract, is_gate_bearing
from relay.domain.story import ActionRefused
from relay.git.checkout import CheckoutFailed, pristine_checkout, trunk_head
from relay.persistence.database import Connection, Pool
from relay.persistence.ledger import append_event
from relay.persistence.outbox import enqueue
from relay.testing.oracle import ProcessOracle
from relay.transport.envelope import Envelope, MessageKind


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
        checks, and end it."""
        async with self._pool.connection() as connection:
            owed = await (
                await connection.execute(
                    "SELECT a.project_id, a.feature_id, p.repository, p.trunk "
                    "FROM assignments a JOIN projects p USING (project_id) "
                    "WHERE a.assignment_id = %s AND a.kind = 'complete'",
                    (assignment_id,),
                )
            ).fetchone()
        if owed is None:
            raise LookupError(f"no completion is owed under {assignment_id}")
        if not is_gate_bearing(self._oracle.isolation.name):
            raise ActionRefused(
                f"{self._oracle.isolation.name} execution cannot complete a feature; "
                "checks must be observed in a container"
            )
        head = await trunk_head(owed["repository"], owed["trunk"], timeout=self._remote_timeout)
        batch = await self._start(assignment_id, owed["project_id"], owed["feature_id"], head)
        try:
            await self._run_checks(batch, owed["repository"])
        except BaseException:
            await self._end(batch["batch_id"], cut_short=True)
            raise
        return await self._end(batch["batch_id"])

    async def _start(
        self, assignment_id: uuid.UUID, project_id: str, feature_id: str, head: str
    ) -> dict[str, Any]:
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
            await _locked_feature(connection, project_id, feature_id)
            assignment = await (
                await connection.execute(
                    "SELECT issued_on->>'request_id' AS request_id FROM assignments "
                    "WHERE assignment_id = %s FOR UPDATE",
                    (assignment_id,),
                )
            ).fetchone()
            assert assignment is not None
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

    async def _end(self, batch_id: uuid.UUID, *, cut_short: bool = False) -> CompletionReceipt:
        """The one rule that ends a generation, whoever calls it."""
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
                    "SELECT b.*, r.state AS request_state FROM completion_batches b "
                    "JOIN completion_requests r USING (request_id) "
                    "WHERE b.batch_id = %s FOR UPDATE OF b, r",
                    (batch_id,),
                )
            ).fetchone()
            assert batch is not None
            if batch["state"] != "running" or batch["request_state"] != "live":
                return CompletionReceipt(batch["state"], batch_id, batch["reason"] or "")
            receipts = await (
                await connection.execute(
                    "SELECT r.obligation_id::text, r.digest, r.revision, r.satisfied, "
                    "r.execution_id::text, o.name, o.source, o.behaviour_id "
                    "FROM completion_receipts r JOIN obligations o USING (obligation_id) "
                    "WHERE r.batch_id = %s ORDER BY o.created_at",
                    (batch_id,),
                )
            ).fetchall()
            observed = [
                {"obligation_id": each["obligation_id"], "digest": each["digest"]}
                for each in receipts
                if each["revision"] == batch["trunk"]
            ]
            if cut_short or observed != batch["snapshot"]["obligations"]:
                return await self._owed_again(
                    connection, batch, "interrupted", "the checks could not all be run"
                )
            try:
                head = await trunk_head(
                    located["repository"], located["trunk"], timeout=self._remote_timeout
                )
            except CheckoutFailed:
                return await self._owed_again(
                    connection, batch, "deferred", "trunk could not be read"
                )
            if head != batch["trunk"]:
                return await self._owed_again(
                    connection, batch, "discarded", "trunk moved while the checks were running"
                )
            assert all(each["satisfied"] for each in receipts)
            return await _complete(connection, batch, receipts)

    async def _owed_again(
        self, connection: Connection, batch: dict[str, Any], state: str, reason: str
    ) -> CompletionReceipt:
        """This generation decided nothing; one new completion is owed for its request."""
        await _close(connection, batch, state, reason)
        await owe_completion(
            connection, batch["project_id"], batch["feature_id"], batch["request_id"]
        )
        return CompletionReceipt(state, batch["batch_id"], reason)


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
            "SELECT s.story_id, s.title, s.state, s.revision, c.story_id AS corrected_by, "
            "g.gate_id::text, g.manifest_digest FROM stories s "
            "LEFT JOIN stories c ON c.project_id = s.project_id AND c.corrects = s.story_id "
            "LEFT JOIN gates g ON g.project_id = s.project_id AND g.story_id = s.story_id "
            "AND g.kind = 'story' AND g.state = 'decided' AND g.outcome = 'approved' "
            "WHERE s.project_id = %s AND s.feature_id = %s ORDER BY s.roadmap_revision, s.position",
            (project_id, feature_id),
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
    return {
        "feature_id": feature_id,
        "problem": feature["problem"],
        "roadmap_revision": batch["snapshot"]["roadmap_revision"],
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
        "checked": [
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
        ],
        # Not measured yet, and never shown as zero.
        "usage": "unknown",
    }


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
