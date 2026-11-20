"""Review of a verified green: one cycle per green, one task per required reviewer.

Reviewers are callers of this API. Nothing here trusts what a caller says about who it
is or what it looked at: authority comes from a capability delivered only in the review
command, and what was reviewed is a tree this code prepared from the pinned commit.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from relay.application.closure import closure_of_behaviour, lock_gate, require_open
from relay.application.stories import require_story_at_work
from relay.domain.behaviour import Behaviour, BehaviourState
from relay.domain.identifiers import require_identifier
from relay.domain.review import (
    Carried,
    Finding,
    FindingStatus,
    Gate,
    GateState,
    ReviewPolicy,
    Severity,
    SubmissionRefused,
    judge_gate,
    require_authority_over,
)
from relay.git.checkout import (
    CheckoutFailed,
    RemoteRejected,
    RemoteUnavailable,
    refs_under,
    resolve_ref,
)
from relay.git.mirror import Mirror
from relay.persistence.database import Connection, Pool
from relay.persistence.ledger import append_event
from relay.persistence.outbox import enqueue
from relay.transport.envelope import Envelope, MessageKind

CONTROL_PLANE = "relay/control-plane"


@dataclass(frozen=True)
class NewFinding:
    severity: Severity
    claim: str
    evidence: str
    remediation: str
    affected_files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("claim", "evidence", "remediation"):
            if not getattr(self, name).strip():
                raise SubmissionRefused(f"a finding needs a {name}")


@dataclass(frozen=True)
class Review:
    """What one reviewer says about one candidate."""

    findings: tuple[NewFinding, ...] = ()
    carried: Mapping[str, Carried] = field(default_factory=dict)
    withdrawal_reasons: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for finding_id, verdict in self.carried.items():
            if (
                verdict is Carried.WITHDRAWN
                and not self.withdrawal_reasons.get(finding_id, "").strip()
            ):
                raise SubmissionRefused(f"withdrawing {finding_id} needs a reason")

    def to_document(self) -> dict[str, Any]:
        return {
            "findings": [
                {
                    "severity": finding.severity.name.lower(),
                    "claim": finding.claim,
                    "evidence": finding.evidence,
                    "remediation": finding.remediation,
                    "affected_files": list(finding.affected_files),
                }
                for finding in self.findings
            ],
            "carried": {
                finding_id: verdict.value for finding_id, verdict in sorted(self.carried.items())
            },
            "withdrawal_reasons": dict(sorted(self.withdrawal_reasons.items())),
        }

    @property
    def digest(self) -> str:
        canonical = json.dumps(self.to_document(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Receipt:
    submission_id: uuid.UUID
    disposition: str
    finding_ids: tuple[str, ...]
    cycle_is_current: bool


class UnknownCapability(Exception):
    """The caller presented nothing that identifies a review task."""


def candidate_ref(project_id: str, behaviour_id: str, cycle_id: uuid.UUID) -> str:
    """Named by the cycle, not the attempt: the same attempt can be verified twice."""
    require_identifier("project", project_id)
    require_identifier("behaviour", behaviour_id)
    return f"refs/relay/candidates/{project_id}/{behaviour_id}/{cycle_id}"


async def open_cycle(
    connection: Connection,
    project_id: str,
    behaviour_id: str,
    *,
    green_execution_id: uuid.UUID,
    red_execution_id: str,
    candidate: str,
) -> uuid.UUID:
    """Called in the transaction that accepts a green. Creates the cycle and its tasks.
    Nothing is announced yet: review commands carry a capability, and are only enqueued
    once the candidate is published."""
    policy = await (
        await connection.execute(
            "SELECT required_reviewers, blocking_from FROM projects WHERE project_id = %s",
            (project_id,),
        )
    ).fetchone()
    assert policy is not None
    await connection.execute(
        "UPDATE review_cycles SET state = 'superseded' "
        "WHERE project_id = %s AND behaviour_id = %s AND state IN ('open', 'satisfied')",
        (project_id, behaviour_id),
    )
    cycle_id = uuid.uuid7()
    await connection.execute(
        "INSERT INTO review_cycles (cycle_id, project_id, behaviour_id, green_execution_id, "
        "red_execution_id, candidate_revision, candidate_ref, required_roles, blocking_from) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            cycle_id,
            project_id,
            behaviour_id,
            green_execution_id,
            red_execution_id,
            candidate,
            candidate_ref(project_id, behaviour_id, cycle_id),
            policy["required_reviewers"],
            policy["blocking_from"],
        ),
    )
    for role in policy["required_reviewers"]:
        await connection.execute(
            "INSERT INTO review_tasks (task_id, cycle_id, role) VALUES (%s, %s, %s)",
            (uuid.uuid7(), cycle_id, role),
        )
    return cycle_id


class Reviews:
    def __init__(self, pool: Pool, mirrors: Path) -> None:
        self._pool = pool
        self._mirrors = mirrors

    def _mirror(self, project_id: str) -> Mirror:
        return Mirror(self._mirrors / f"{require_identifier('project', project_id)}.git")

    async def publish_candidates(self) -> int:
        """Publish every candidate still pending, then announce its review tasks. Safe to
        run any number of times, and after a crash at any point."""
        async with self._pool.connection() as connection:
            pending = await (
                await connection.execute(
                    "SELECT c.*, p.repository FROM review_cycles c JOIN projects p USING (project_id) "
                    "JOIN behaviours b USING (project_id, behaviour_id) "
                    "JOIN stories s USING (project_id, story_id) "
                    "JOIN features f USING (project_id, feature_id) "
                    "WHERE c.publication = 'pending' AND c.state = 'open' "
                    "AND f.closed_by IS NULL ORDER BY c.created_at"
                )
            ).fetchall()
        published = 0
        for cycle in pending:
            try:
                published += await self._publish(cycle)
            except CheckoutFailed as failure:
                # One project's Git trouble must not hold up the others.
                await self._defer(cycle, str(failure)[:500])
        return published

    async def _publish(self, cycle: dict[str, Any]) -> int:
        mirror = self._mirror(cycle["project_id"])
        commit, ref = cycle["candidate_revision"], cycle["candidate_ref"]
        repository = cycle["repository"]
        try:
            if not await self._recover(cycle):
                await self._block(cycle["cycle_id"], _COMMIT_LOST)
                return 0
            if await resolve_ref(repository, ref) is None:
                try:
                    await mirror.publish(repository, commit, ref)
                except RemoteRejected as refused:
                    # Two publishers racing for the same ref: the loser is refused, yet
                    # the ref is now exactly right. Only a refusal that leaves the
                    # candidate unpublished is the remote saying no to it.
                    if await resolve_ref(repository, ref) != commit:
                        reason = f"the remote refused the candidate: {refused}"
                        await self._block(cycle["cycle_id"], reason[:500])
                        return 0
                except RemoteUnavailable:
                    raise
                except CheckoutFailed:
                    pass  # the ref appeared meanwhile; what it names is checked next
            published_as = await resolve_ref(repository, ref)
            if published_as is None:
                raise RemoteUnavailable("the candidate ref is still absent after the push")
            if published_as != commit:
                await self._block(cycle["cycle_id"], "the candidate ref names another commit")
                return 0
        except RemoteUnavailable as unavailable:
            await self._defer(cycle, str(unavailable)[:500])
            return 0
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, cycle["project_id"])
            current = await (
                await connection.execute(
                    "SELECT publication, state FROM review_cycles WHERE cycle_id = %s FOR UPDATE",
                    (cycle["cycle_id"],),
                )
            ).fetchone()
            assert current is not None
            if current["publication"] != "pending" or current["state"] != "open":
                return 0
            if await closure_of_behaviour(connection, cycle["project_id"], cycle["behaviour_id"]):
                # Closed while the candidate was on its way to the repository. The ref is
                # there; announcing it to reviewers waits for the feature to reopen.
                return 0
            await connection.execute(
                "UPDATE review_cycles SET publication = 'published', deferred_reason = NULL "
                "WHERE cycle_id = %s",
                (cycle["cycle_id"],),
            )
            published = await append_event(
                connection,
                cycle["project_id"],
                "review.candidate_published",
                subject_id=cycle["behaviour_id"],
                payload={
                    "cycle_id": str(cycle["cycle_id"]),
                    "candidate_revision": commit,
                    "ref": ref,
                },
            )
            tasks = await (
                await connection.execute(
                    "SELECT task_id, role FROM review_tasks WHERE cycle_id = %s ORDER BY role",
                    (cycle["cycle_id"],),
                )
            ).fetchall()
            for task in tasks:
                capability = secrets.token_urlsafe(32)
                command = _review_command(
                    cycle, task["task_id"], task["role"], capability, published.event_id
                )
                await connection.execute(
                    "UPDATE review_tasks SET capability = %s, command_message_id = %s "
                    "WHERE task_id = %s",
                    (capability, command.message_id, task["task_id"]),
                )
                await enqueue(
                    connection, f"relay:{cycle['project_id']}:cmd:{task['role']}", command
                )
        return 1

    async def adopt_reviews_without_a_cycle(self) -> int:
        """A behaviour can be in review with no cycle, if it got there before cycles
        existed. Give it one, from the green and the commit already on record. If that
        commit can no longer be obtained, block the behaviour and say so."""
        async with self._pool.connection() as connection:
            orphans = await (
                await connection.execute(
                    "SELECT b.project_id, b.behaviour_id, b.candidate_revision, "
                    "b.red_execution_id::text, p.repository FROM behaviours b "
                    "JOIN projects p USING (project_id) WHERE b.state = 'review' AND NOT EXISTS "
                    "(SELECT 1 FROM review_cycles c WHERE c.project_id = b.project_id "
                    "AND c.behaviour_id = b.behaviour_id AND c.state IN ('open', 'satisfied'))"
                )
            ).fetchall()
        adopted = 0
        for orphan in orphans:
            adopted += await self._adopt(orphan)
        return adopted

    async def _adopt(self, orphan: dict[str, Any]) -> int:
        project_id, behaviour_id = orphan["project_id"], orphan["behaviour_id"]
        commit = orphan["candidate_revision"]
        try:
            recovered = await self._recover(orphan)
        except CheckoutFailed as failure:
            # Nothing is known yet. Say why, once, and leave this behaviour for the next
            # pass; other behaviours must not wait on it.
            await self._adoption_deferred(project_id, behaviour_id, str(failure)[:500])
            return 0
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
            row = await (
                await connection.execute(
                    "SELECT state FROM behaviours WHERE project_id = %s "
                    "AND behaviour_id = %s FOR UPDATE",
                    (project_id, behaviour_id),
                )
            ).fetchone()
            live = await (
                await connection.execute(
                    "SELECT 1 FROM review_cycles WHERE project_id = %s AND behaviour_id = %s "
                    "AND state IN ('open', 'satisfied')",
                    (project_id, behaviour_id),
                )
            ).fetchone()
            if row is None or row["state"] != "review" or live is not None:
                return 0
            green = await _accepted_green(connection, project_id, behaviour_id, commit)
            if not recovered or green is None:
                await connection.execute(
                    "UPDATE behaviours SET state = 'blocked' "
                    "WHERE project_id = %s AND behaviour_id = %s",
                    (project_id, behaviour_id),
                )
                await append_event(
                    connection,
                    project_id,
                    "review.recovery_blocked",
                    subject_id=behaviour_id,
                    payload={
                        "candidate_revision": commit,
                        "reason": _COMMIT_LOST if not recovered else _NO_ACCEPTED_GREEN,
                    },
                )
                return 0
            cycle_id = await open_cycle(
                connection,
                project_id,
                behaviour_id,
                green_execution_id=green,
                red_execution_id=orphan["red_execution_id"],
                candidate=commit,
            )
            await append_event(
                connection,
                project_id,
                "review.cycle_adopted",
                subject_id=behaviour_id,
                payload={
                    "cycle_id": str(cycle_id),
                    "candidate_revision": commit,
                    "green_execution_id": str(green),
                },
            )
        return 1

    async def _adoption_deferred(self, project_id: str, behaviour_id: str, reason: str) -> None:
        """Say once why a review could not be adopted yet. Under the behaviour's lock, and
        only if it still needs adopting: another reconciler may have adopted it meanwhile,
        and the ledger must not then end on "still pending"."""
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
            behaviour = await (
                await connection.execute(
                    "SELECT state FROM behaviours WHERE project_id = %s AND behaviour_id = %s "
                    "FOR UPDATE",
                    (project_id, behaviour_id),
                )
            ).fetchone()
            live = await (
                await connection.execute(
                    "SELECT 1 FROM review_cycles WHERE project_id = %s AND behaviour_id = %s "
                    "AND state IN ('open', 'satisfied')",
                    (project_id, behaviour_id),
                )
            ).fetchone()
            if behaviour is None or behaviour["state"] != "review" or live is not None:
                return
            latest = await (
                await connection.execute(
                    "SELECT event_type, payload FROM events WHERE project_id = %s "
                    "AND subject_id = %s ORDER BY sequence DESC LIMIT 1",
                    (project_id, behaviour_id),
                )
            ).fetchone()
            already_said = (
                latest is not None
                and latest["event_type"] == "review.adoption_deferred"
                and latest["payload"].get("reason") == reason
            )
            if not already_said:
                await append_event(
                    connection,
                    project_id,
                    "review.adoption_deferred",
                    subject_id=behaviour_id,
                    payload={"reason": reason},
                )

    async def _recover(self, record: Mapping[str, Any]) -> bool:
        """The one way a judged commit is brought back: from the control plane's pin, or
        else from any place the repository may still have it, whether the published
        candidate ref or a Builder's attempt whose history contains it."""
        project_id, behaviour_id = record["project_id"], record["behaviour_id"]
        if await self._mirror(project_id).holds(record["candidate_revision"]):
            return True
        attempts = await refs_under(
            record["repository"], f"refs/relay/attempts/{project_id}/{behaviour_id}/"
        )
        # The cycle knows the candidate ref's name from the start, but the ref exists only
        # once published. Searching a ref that is not there would look like an unfinished
        # search and leave a commit that is truly gone pending for ever.
        named = record.get("candidate_ref")
        published = [named] if named and await resolve_ref(record["repository"], named) else []
        return await self._mirror(project_id).recover(
            record["repository"], record["candidate_revision"], [*published, *sorted(attempts)]
        )

    async def announce_unreviewed_tasks(self) -> int:
        """Send again the command of every task still waiting for its review. It is the
        same message with the same identity, so a consumer that has already acted on it
        does nothing new."""
        async with self._pool.connection() as connection:
            projects = await (
                await connection.execute("SELECT project_id FROM projects ORDER BY project_id")
            ).fetchall()
        announced = 0
        for project in projects:
            async with self._pool.connection() as connection, connection.transaction():
                await lock_gate(connection, project["project_id"])
                again = await (
                    await connection.execute(
                        "UPDATE outbox SET published_at = NULL WHERE published_at IS NOT NULL "
                        "AND message_id IN (SELECT t.command_message_id FROM review_tasks t "
                        "JOIN review_cycles c USING (cycle_id) "
                        "JOIN behaviours b USING (project_id, behaviour_id) "
                        "JOIN stories st USING (project_id, story_id) "
                        "JOIN features f USING (project_id, feature_id) "
                        "WHERE c.project_id = %s AND c.state = 'open' "
                        "AND c.publication = 'published' AND f.closed_by IS NULL "
                        "AND NOT EXISTS (SELECT 1 FROM review_submissions s "
                        "WHERE s.task_id = t.task_id AND s.disposition = 'effective')) "
                        "RETURNING message_id",
                        (project["project_id"],),
                    )
                ).fetchall()
            announced += len(again)
        return announced

    async def prepare_review(self, capability: str, into: Path) -> Path:
        """Give a reviewer the tree to review: exactly the cycle's commit, from the control
        plane's pinned copy, after confirming the published ref still names that commit."""
        task = await self._task(capability)
        if task["state"] != "open" or task["publication"] != "published":
            raise SubmissionRefused("this review is no longer open")
        async with self._pool.connection() as connection:
            require_open(
                await closure_of_behaviour(connection, task["project_id"], task["behaviour_id"])
            )
        commit = task["candidate_revision"]
        try:
            names_the_commit = (
                await resolve_ref(task["repository"], task["candidate_ref"]) == commit
            )
            recovered = names_the_commit and await self._recover(task)
        except RemoteUnavailable as unavailable:
            raise SubmissionRefused(f"the repository cannot be reached: {unavailable}") from None
        if not names_the_commit:
            await self._block(
                task["cycle_id"], "the candidate ref no longer names the reviewed commit"
            )
            raise SubmissionRefused("the candidate ref no longer names the commit under review")
        if not recovered:
            await self._block(task["cycle_id"], _COMMIT_LOST)
            raise SubmissionRefused(_COMMIT_LOST)
        tree = await self._mirror(task["project_id"]).export(commit, into)
        async with self._pool.connection() as connection, connection.transaction():
            # The receipt is what starts a review. A closure that committed while the
            # tree was being exported is seen here, and then no review has started.
            await lock_gate(connection, task["project_id"])
            require_open(
                await closure_of_behaviour(connection, task["project_id"], task["behaviour_id"])
            )
            await connection.execute(
                "INSERT INTO review_checkouts (receipt_id, task_id, revision) VALUES (%s, %s, %s)",
                (uuid.uuid7(), task["task_id"], commit),
            )
        return tree

    async def submit_review(self, capability: str, review: Review) -> Receipt:
        """Record one reviewer's review. Every request gets exactly one answer however often
        it is retried, and only the first accepted submission of a task has any effect."""
        task_id = (await self._task(capability))["task_id"]
        async with self._pool.connection() as connection, connection.transaction():
            task = await _locked_task(connection, task_id)
            accepting = task["state"] == "open"
            is_current = task["state"] in ("open", "satisfied")
            effective = await (
                await connection.execute(
                    "SELECT submission_id, content_digest, finding_ids FROM review_submissions "
                    "WHERE task_id = %s AND disposition = 'effective'",
                    (task_id,),
                )
            ).fetchone()
            if effective is not None:
                if effective["content_digest"] == review.digest:
                    return Receipt(
                        effective["submission_id"],
                        "effective",
                        tuple(effective["finding_ids"]),
                        cycle_is_current=is_current,
                    )
                return await _store(connection, task, review, "conflicting", is_current)
            if not accepting:
                return await _store(connection, task, review, "stale", is_current)
            if not await _has_checkout(connection, task_id):
                raise SubmissionRefused("no tree was prepared for this review")
            return await self._accept(connection, task, review)

    async def dispute(self, project_id: str, finding_id: str, evidence: str) -> None:
        """The Builder disagrees, with evidence. The finding stays blocking: only the role
        that raised it can settle it."""
        if not evidence.strip():
            raise SubmissionRefused("a dispute needs evidence")
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
            behaviour = await (
                await connection.execute(
                    "SELECT b.* FROM behaviours b JOIN findings f USING (project_id, behaviour_id) "
                    "WHERE f.finding_id = %s AND f.project_id = %s FOR UPDATE OF b",
                    (finding_id, project_id),
                )
            ).fetchone()
            if behaviour is None:
                raise SubmissionRefused(f"{finding_id} cannot be disputed")
            # Once the story is at its gate the Product Owner is looking at a review that
            # says how each finding stands. It must not change behind that review.
            await require_story_at_work(connection, project_id, behaviour, entering_design=False)
            row = await (
                await connection.execute(
                    "UPDATE findings SET status = 'disputed', dispute = %s "
                    "WHERE finding_id = %s AND project_id = %s AND status <> 'withdrawn' "
                    "RETURNING behaviour_id",
                    (evidence, finding_id, project_id),
                )
            ).fetchone()
            if row is None:
                raise SubmissionRefused(f"{finding_id} cannot be disputed")
            await append_event(
                connection,
                project_id,
                "review.finding_disputed",
                subject_id=row["behaviour_id"],
                payload={"finding_id": finding_id, "evidence": evidence},
            )

    async def _accept(
        self, connection: Connection, task: dict[str, Any], review: Review
    ) -> Receipt:
        project_id, behaviour_id, cycle_id = (
            task["project_id"],
            task["behaviour_id"],
            task["cycle_id"],
        )
        known = await _findings(connection, project_id, behaviour_id)
        require_authority_over(task["role"], review.carried, known)
        finding_ids = []
        for finding in review.findings:
            finding_id = f"F-{uuid.uuid4()}"
            finding_ids.append(finding_id)
            await connection.execute(
                "INSERT INTO findings (finding_id, project_id, behaviour_id, role, severity, claim, "
                "evidence, affected_files, remediation, raised_in_cycle) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    finding_id,
                    project_id,
                    behaviour_id,
                    task["role"],
                    int(finding.severity),
                    finding.claim,
                    finding.evidence,
                    list(finding.affected_files),
                    finding.remediation,
                    cycle_id,
                ),
            )
        for finding_id, verdict in review.carried.items():
            if verdict is Carried.VERIFIED_FIXED:
                await connection.execute(
                    "INSERT INTO finding_verifications (finding_id, cycle_id) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (finding_id, cycle_id),
                )
            elif verdict is Carried.WITHDRAWN:
                await connection.execute(
                    "UPDATE findings SET status = 'withdrawn', withdrawal_reason = %s "
                    "WHERE finding_id = %s",
                    (review.withdrawal_reasons[finding_id], finding_id),
                )
        receipt = await _store(connection, task, review, "effective", True, tuple(finding_ids))
        submitted = await append_event(
            connection,
            project_id,
            "review.submitted",
            subject_id=behaviour_id,
            payload={
                "cycle_id": str(cycle_id),
                "role": task["role"],
                "submission_id": str(receipt.submission_id),
                "finding_ids": finding_ids,
                "carried": {key: value.value for key, value in review.carried.items()},
            },
        )
        await self._apply_gate(connection, task, submitted.event_id)
        return receipt

    async def _apply_gate(
        self, connection: Connection, task: dict[str, Any], cause: uuid.UUID
    ) -> None:
        project_id, behaviour_id, cycle_id = (
            task["project_id"],
            task["behaviour_id"],
            task["cycle_id"],
        )
        gate = await gate_of(connection, task)
        if gate.state is GateState.AWAITING_REVIEWS:
            return
        behaviour_row = await (
            await connection.execute(
                "SELECT * FROM behaviours WHERE project_id = %s AND behaviour_id = %s",
                (project_id, behaviour_id),
            )
        ).fetchone()
        assert behaviour_row is not None
        behaviour = Behaviour(
            behaviour_id, behaviour_row["summary"], BehaviourState(behaviour_row["state"])
        )
        payload: dict[str, Any] = {"cycle_id": str(cycle_id), "blocking": list(gate.blocking)}
        if gate.state is GateState.SATISFIED:
            event_type = "review.gate_satisfied"
        elif gate.state is GateState.CHANGES_REQUESTED:
            behaviour.request_changes()
            event_type = "review.changes_requested"
            await connection.execute(
                "INSERT INTO remediation_requests (cycle_id, finding_ids) VALUES (%s, %s)",
                (cycle_id, list(gate.blocking)),
            )
        else:
            behaviour.park()
            event_type = "review.parked"
            payload["reason"] = gate.reason
        await connection.execute(
            "UPDATE review_cycles SET state = %s, blocked_reason = %s WHERE cycle_id = %s",
            (gate.state.value, gate.reason or None, cycle_id),
        )
        await connection.execute(
            "UPDATE behaviours SET state = %s WHERE project_id = %s AND behaviour_id = %s",
            (behaviour.state.value, project_id, behaviour_id),
        )
        decided = await append_event(
            connection,
            project_id,
            event_type,
            subject_id=behaviour_id,
            payload=payload,
            causation_id=cause,
        )
        if gate.state is GateState.CHANGES_REQUESTED:
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
                    correlation_id=behaviour_row["correlation_id"],
                    causation_id=decided.event_id,
                    payload={
                        "base_revision": task["candidate_revision"],
                        "remediate_findings": list(gate.blocking),
                    },
                ),
            )

    async def _task(self, capability: str) -> dict[str, Any]:
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(_TASK_QUERY + " WHERE t.capability = %s", (capability,))
            ).fetchone()
        if row is None or not hmac.compare_digest(row["capability"], capability):
            raise UnknownCapability("no review task answers to that capability")
        return row

    async def _block(self, cycle_id: uuid.UUID, reason: str) -> None:
        """Stop a cycle that can no longer be trusted. Locks are taken in the same order
        as a submission takes them (behaviour, then cycle), and the cycle's state is read
        again under them, so the two cannot deadlock or overwrite each other."""
        async with self._pool.connection() as connection, connection.transaction():
            located = await (
                await connection.execute(
                    "SELECT project_id, behaviour_id FROM review_cycles WHERE cycle_id = %s",
                    (cycle_id,),
                )
            ).fetchone()
            assert located is not None
            project_id, behaviour_id = located["project_id"], located["behaviour_id"]
            await lock_gate(connection, project_id)
            await connection.execute(
                "SELECT 1 FROM behaviours WHERE project_id = %s AND behaviour_id = %s FOR UPDATE",
                (project_id, behaviour_id),
            )
            stopped = await (
                await connection.execute(
                    # A cycle whose integration has been authorised is no longer this
                    # code's to stop: the intent owns what happens to it now.
                    "UPDATE review_cycles SET state = 'blocked', blocked_reason = %s "
                    "WHERE cycle_id = %s AND state IN ('open', 'satisfied') AND NOT EXISTS "
                    "(SELECT 1 FROM integration_intents i WHERE i.cycle_id = %s "
                    "AND i.state = 'outstanding') RETURNING cycle_id",
                    (reason, cycle_id, cycle_id),
                )
            ).fetchone()
            if stopped is not None:
                await connection.execute(
                    "UPDATE behaviours SET state = 'blocked' "
                    "WHERE project_id = %s AND behaviour_id = %s AND state = 'review'",
                    (project_id, behaviour_id),
                )
            else:
                await connection.execute(
                    "UPDATE review_cycles SET conflict_observed = %s "
                    "WHERE cycle_id = %s AND state IN ('open', 'satisfied')",
                    (reason, cycle_id),
                )
            await append_event(
                connection,
                project_id,
                "review.cycle_blocked" if stopped is not None else "review.conflict_observed",
                subject_id=behaviour_id,
                payload={"cycle_id": str(cycle_id), "reason": reason},
            )

    async def _defer(self, cycle: dict[str, Any], reason: str) -> None:
        """Publication could not be attempted or confirmed. The cycle stays pending and
        the reason is recorded once, not on every retry."""
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, cycle["project_id"])
            changed = await (
                await connection.execute(
                    "UPDATE review_cycles SET deferred_reason = %s WHERE cycle_id = %s "
                    "AND publication = 'pending' AND deferred_reason IS DISTINCT FROM %s "
                    "RETURNING cycle_id",
                    (reason, cycle["cycle_id"], reason),
                )
            ).fetchone()
            if changed is not None:
                await append_event(
                    connection,
                    cycle["project_id"],
                    "review.publication_deferred",
                    subject_id=cycle["behaviour_id"],
                    payload={"cycle_id": str(cycle["cycle_id"]), "reason": reason},
                )


_COMMIT_LOST = "the commit that was judged green can no longer be obtained"
_NO_ACCEPTED_GREEN = "no accepted green is on record for the commit in review"


async def _accepted_green(
    connection: Connection, project_id: str, behaviour_id: str, commit: str
) -> uuid.UUID | None:
    """The execution the ledger says was verified green for this commit. Other runs of the
    same commit exist too (rejected, superseded); only the ledger says which one counted."""
    row = await (
        await connection.execute(
            "SELECT e.execution_id FROM events v JOIN executions e "
            "ON e.execution_id = (v.payload->>'execution_id')::uuid "
            "WHERE v.project_id = %s AND v.subject_id = %s "
            "AND v.event_type = 'behaviour.green_verified' "
            "AND v.payload->>'candidate_revision' = %s AND e.purpose = 'green' "
            "AND e.revision = %s AND e.project_id = %s AND e.behaviour_id = %s "
            "AND e.contract_digest = v.payload->>'contract_digest' "
            "ORDER BY v.sequence DESC LIMIT 1",
            (project_id, behaviour_id, commit, commit, project_id, behaviour_id),
        )
    ).fetchone()
    return row["execution_id"] if row else None


_TASK_QUERY = (
    "SELECT t.task_id, t.role, t.capability, c.cycle_id, c.project_id, c.behaviour_id, c.state, "
    "c.publication, c.candidate_revision, c.candidate_ref, c.required_roles, c.blocking_from, "
    "p.repository FROM review_tasks t JOIN review_cycles c USING (cycle_id) "
    "JOIN projects p USING (project_id)"
)


async def _locked_task(connection: Connection, task_id: uuid.UUID) -> dict[str, Any]:
    """Re-read the task under the behaviour's row lock, so the cycle cannot change beneath it."""
    task = await (
        await connection.execute(_TASK_QUERY + " WHERE t.task_id = %s", (task_id,))
    ).fetchone()
    assert task is not None
    await lock_gate(connection, task["project_id"])
    await connection.execute(
        "SELECT 1 FROM behaviours WHERE project_id = %s AND behaviour_id = %s FOR UPDATE",
        (task["project_id"], task["behaviour_id"]),
    )
    task = await (
        await connection.execute(_TASK_QUERY + " WHERE t.task_id = %s", (task_id,))
    ).fetchone()
    assert task is not None
    return task


async def _has_checkout(connection: Connection, task_id: uuid.UUID) -> bool:
    row = await (
        await connection.execute(
            "SELECT 1 FROM review_checkouts WHERE task_id = %s LIMIT 1", (task_id,)
        )
    ).fetchone()
    return row is not None


async def _store(
    connection: Connection,
    task: dict[str, Any],
    review: Review,
    disposition: str,
    cycle_is_current: bool,
    finding_ids: tuple[str, ...] = (),
) -> Receipt:
    submission_id = uuid.uuid7()
    await connection.execute(
        "INSERT INTO review_submissions (submission_id, task_id, content_digest, content, "
        "disposition, finding_ids) VALUES (%s, %s, %s, %s, %s, %s)",
        (
            submission_id,
            task["task_id"],
            review.digest,
            Jsonb(review.to_document()),
            disposition,
            list(finding_ids),
        ),
    )
    if disposition != "effective":
        await append_event(
            connection,
            task["project_id"],
            f"review.submission_{disposition}",
            subject_id=task["behaviour_id"],
            payload={
                "cycle_id": str(task["cycle_id"]),
                "role": task["role"],
                "submission_id": str(submission_id),
            },
        )
    return Receipt(submission_id, disposition, finding_ids, cycle_is_current)


async def _findings(connection: Connection, project_id: str, behaviour_id: str) -> list[Finding]:
    rows = await (
        await connection.execute(
            "SELECT finding_id, role, severity, status FROM findings "
            "WHERE project_id = %s AND behaviour_id = %s",
            (project_id, behaviour_id),
        )
    ).fetchall()
    return [
        Finding(
            row["finding_id"], row["role"], Severity(row["severity"]), FindingStatus(row["status"])
        )
        for row in rows
    ]


async def gate_of(connection: Connection, task: Mapping[str, Any]) -> Gate:
    """The gate of one cycle as it stands now. `task` is any row carrying the cycle's
    id, policy, project and behaviour."""
    submitted = await (
        await connection.execute(
            "SELECT t.role FROM review_submissions s JOIN review_tasks t USING (task_id) "
            "WHERE t.cycle_id = %s AND s.disposition = 'effective'",
            (task["cycle_id"],),
        )
    ).fetchall()
    verified = await (
        await connection.execute(
            "SELECT finding_id FROM finding_verifications WHERE cycle_id = %s", (task["cycle_id"],)
        )
    ).fetchall()
    return judge_gate(
        ReviewPolicy(frozenset(task["required_roles"]), Severity(task["blocking_from"])),
        {row["role"] for row in submitted},
        await _findings(connection, task["project_id"], task["behaviour_id"]),
        {row["finding_id"] for row in verified},
    )


def _review_command(
    cycle: dict[str, Any],
    task_id: uuid.UUID,
    role: str,
    capability: str,
    cause: uuid.UUID,
) -> Envelope:
    return Envelope(
        message_kind=MessageKind.COMMAND,
        event_type="review.requested",
        project_id=cycle["project_id"],
        behaviour_id=cycle["behaviour_id"],
        sender=CONTROL_PLANE,
        recipient=role,
        idempotency_key=f"review:{task_id}",
        correlation_id=uuid.uuid7(),
        causation_id=cause,
        payload={
            "task_id": str(task_id),
            "capability": capability,
            "candidate_revision": cycle["candidate_revision"],
            "candidate_ref": cycle["candidate_ref"],
        },
    )
