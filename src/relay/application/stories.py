"""Stories, roadmaps and the gates at which only the Product Owner decides.

Everything here is state the control plane owns. Roles propose (a roadmap, a sealed list
of behaviours, an assessment); code decides what that makes possible; and a gate is
settled by a person, on the exact review that person was shown.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from psycopg.types.json import Jsonb

from relay.domain.evidence import ProcessContract
from relay.domain.identifiers import require_identifier
from relay.domain.story import (
    ActionRefused,
    Candidate,
    HumanAction,
    Principal,
    PrincipalKind,
    Receipt,
    StoryState,
    answer_again,
    next_story_to_release,
    permits_behaviour_work,
    require_human,
    require_sealable,
    require_well_formed,
)
from relay.persistence.database import Connection, Pool
from relay.persistence.ledger import append_event
from relay.persistence.outbox import enqueue
from relay.transport.envelope import Envelope, MessageKind

CONTROL_PLANE = "relay/control-plane"


@dataclass(frozen=True)
class StoryDraft:
    story_id: str
    title: str
    outcome: str
    criteria: Mapping[str, str]

    def __post_init__(self) -> None:
        # A copy of its own, taken before anything is checked: what is validated here,
        # stored and shown must be one and the same thing.
        criteria = MappingProxyType(dict(self.criteria))
        object.__setattr__(self, "criteria", criteria)
        require_identifier("story", self.story_id)
        if not self.title.strip() or not self.outcome.strip():
            raise ValueError("a story needs a title and the outcome it is for")
        if not criteria:
            raise ValueError("a story needs at least one acceptance criterion")
        for criterion_id, text in criteria.items():
            require_identifier("criterion", criterion_id)
            if not text.strip():
                raise ValueError(f"criterion {criterion_id} says nothing")


@dataclass(frozen=True)
class InboxItem:
    """Something waiting for the Product Owner, with exactly what there is to decide on."""

    gate_id: uuid.UUID
    kind: str
    story_id: str | None
    manifest: dict[str, Any]
    manifest_digest: str


class Stories:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def open_feature(
        self, principal: Principal, project_id: str, feature_id: str, problem: str
    ) -> None:
        require_human(principal)
        if not problem.strip():
            raise ValueError("a feature starts from a stated problem")
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute(
                "INSERT INTO features (project_id, feature_id, problem) VALUES (%s, %s, %s)",
                (project_id, require_identifier("feature", feature_id), problem),
            )
            await append_event(
                connection,
                project_id,
                "feature.opened",
                subject_id=feature_id,
                payload={"problem": problem, "by": principal.identity},
            )

    async def publish_roadmap(
        self, principal: Principal, project_id: str, feature_id: str, stories: Sequence[StoryDraft]
    ) -> uuid.UUID:
        """The Analyst's proposal. Nothing in it is released until a person approves it
        at the gate this opens."""
        _require_role(principal, "analyst")
        # Read once, before anything is awaited. The rows inserted and the review shown
        # are both made from this, so they cannot differ.
        stories = tuple(stories)
        if not stories:
            raise ValueError("a roadmap needs at least one story")
        async with self._pool.connection() as connection, connection.transaction():
            await _locked_project(connection, project_id)
            feature = await _locked_feature(connection, project_id, feature_id)
            existing = await (
                await connection.execute(
                    "SELECT 1 FROM roadmaps WHERE project_id = %s AND feature_id = %s LIMIT 1",
                    (project_id, feature_id),
                )
            ).fetchone()
            if existing is not None:
                raise ActionRefused(f"{feature['feature_id']} already has a roadmap")
            await connection.execute(
                "INSERT INTO roadmaps (project_id, feature_id, revision) VALUES (%s, %s, 1)",
                (project_id, feature_id),
            )
            for position, story in enumerate(stories, start=1):
                await connection.execute(
                    "INSERT INTO stories (project_id, story_id, feature_id, roadmap_revision, "
                    "position, title, outcome, criteria) VALUES (%s, %s, %s, 1, %s, %s, %s, %s)",
                    (
                        project_id,
                        story.story_id,
                        feature_id,
                        position,
                        story.title,
                        story.outcome,
                        Jsonb(dict(story.criteria)),
                    ),
                )
            manifest = {
                "feature_id": feature_id,
                "problem": feature["problem"],
                "roadmap_revision": 1,
                "stories": [
                    {
                        "story_id": story.story_id,
                        "title": story.title,
                        "outcome": story.outcome,
                        "criteria": dict(story.criteria),
                    }
                    for story in stories
                ],
            }
            return await _open_gate(
                connection,
                project_id,
                feature_id,
                kind="roadmap",
                story_id=None,
                basis={"roadmap_revision": 1},
                manifest=manifest,
            )

    async def seal(
        self,
        principal: Principal,
        project_id: str,
        story_id: str,
        *,
        story_revision: int,
        list_revision: int,
    ) -> None:
        """The Verifier vouches that the behaviours of one named list revision, and no
        others, show the story's criteria. A seal that arrives after the list has moved
        on vouches for something that is no longer there, and is refused."""
        _require_role(principal, "verifier")
        async with self._pool.connection() as connection, connection.transaction():
            story = await _locked_story(connection, project_id, story_id)
            _require_at_work(story)
            _require_revisions(story, story_revision, list_revision, story["list_revision"])
            listed = await _behaviours_of(connection, project_id, story_id)
            require_sealable(
                story["criteria"].keys(),
                {row["behaviour_id"]: row["demonstrates"] for row in listed},
            )
            await connection.execute(
                "UPDATE stories SET sealed_revision = list_revision "
                "WHERE project_id = %s AND story_id = %s",
                (project_id, story_id),
            )
            await append_event(
                connection,
                project_id,
                "story.behaviours_sealed",
                subject_id=story_id,
                payload={
                    "list_revision": story["list_revision"],
                    "behaviours": [row["behaviour_id"] for row in listed],
                },
            )
            await evaluate_story_gate(connection, project_id, story_id)

    async def assess(
        self,
        principal: Principal,
        project_id: str,
        story_id: str,
        statement: str,
        *,
        story_revision: int,
        list_revision: int,
    ) -> None:
        """The Analyst says whether one named sealed list demonstrates the outcome. The
        statement is shown to the Product Owner as written, so it must be about the list
        the Analyst read, and is refused if that is no longer the sealed one."""
        _require_role(principal, "analyst")
        if not statement.strip():
            raise ValueError("an assessment says something")
        async with self._pool.connection() as connection, connection.transaction():
            story = await _locked_story(connection, project_id, story_id)
            _require_at_work(story)
            if _is_unsealed(story):
                raise ActionRefused("only a sealed list of behaviours can be assessed")
            _require_revisions(story, story_revision, list_revision, story["sealed_revision"])
            if await _assessment(connection, story) is not None:
                raise ActionRefused("this sealed list has already been assessed")
            await connection.execute(
                "INSERT INTO story_assessments (assessment_id, project_id, story_id, "
                "story_revision, list_revision, statement) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    uuid.uuid7(),
                    project_id,
                    story_id,
                    story["revision"],
                    story["sealed_revision"],
                    statement,
                ),
            )
            await append_event(
                connection,
                project_id,
                "story.assessed",
                subject_id=story_id,
                payload={"list_revision": story["sealed_revision"]},
            )
            await evaluate_story_gate(connection, project_id, story_id)

    async def decide(self, action: HumanAction) -> Receipt:
        """Apply one thing a person asked for on one gate. However often the request
        arrives it is applied once, and what the caller is told is what happened to
        exactly that request."""
        require_human(action.principal)
        require_well_formed(action)
        try:
            gate_id = uuid.UUID(action.target)
        except ValueError:
            raise ActionRefused("no such gate") from None
        async with self._pool.connection() as connection, connection.transaction():
            located = await (
                await connection.execute(
                    "SELECT project_id, feature_id, kind, story_id FROM gates WHERE gate_id = %s",
                    (gate_id,),
                )
            ).fetchone()
            if located is None:
                raise ActionRefused("no such gate")
            project_id = located["project_id"]
            # Story, then project, then feature: the order every transition uses. The
            # project comes before any feature because releasing the next story, under
            # the project lock, may reach into a feature other than this one.
            story = (
                await _locked_story(connection, project_id, located["story_id"])
                if located["story_id"]
                else None
            )
            await _locked_project(connection, project_id)
            await _locked_feature(connection, project_id, located["feature_id"])
            if (seen := answer_again(action, await _recorded(connection, action))) is not None:
                return seen
            gate = await (
                await connection.execute(
                    "SELECT * FROM gates WHERE gate_id = %s FOR UPDATE", (gate_id,)
                )
            ).fetchone()
            assert gate is not None
            current = await _basis_now(connection, gate, story)
            if gate["state"] != "open" or gate["basis"] != current:
                if gate["state"] == "open":
                    # What this gate showed is no longer what there is to decide on.
                    await _close_gate(connection, gate, "superseded", "superseded")
                receipt = Receipt("stale", "nothing changed: this gate is no longer open")
                await _record(connection, action, receipt)
                return receipt
            outcome = "approved" if action.action == "approve" else "changes_requested"
            await _close_gate(connection, gate, "decided", outcome)
            if story is None:
                await _settle_roadmap(connection, gate, outcome)
            else:
                await _settle_story(connection, story, outcome)
            await append_event(
                connection,
                project_id,
                f"gate.{outcome}",
                subject_id=located["story_id"] or located["feature_id"],
                payload={
                    "gate_id": str(gate_id),
                    "kind": gate["kind"],
                    "by": action.principal.identity,
                    "request_id": action.request_id,
                    "manifest_digest": gate["manifest_digest"],
                    # What the person said, kept apart from what Relay records about it.
                    "said": dict(action.payload),
                },
            )
            await release_next(connection, project_id)
            receipt = Receipt("effective", outcome)
            await _record(connection, action, receipt)
            return receipt

    async def inbox(self, project_id: str) -> list[InboxItem]:
        """What is waiting for the Product Owner, read from the durable record."""
        async with self._pool.connection() as connection:
            rows = await (
                await connection.execute(
                    "SELECT g.gate_id, g.kind, g.story_id, g.manifest, g.manifest_digest "
                    "FROM inbox_items i JOIN gates g USING (gate_id) "
                    "WHERE i.project_id = %s AND i.state = 'open' ORDER BY i.opened_at",
                    (project_id,),
                )
            ).fetchall()
        return [InboxItem(**row) for row in rows]

    async def revisions_of(self, project_id: str, story_id: str) -> tuple[int, int]:
        """The story revision and the list revision as they stand. A role reads these
        with what it is about to vouch for, and hands them back with its seal or its
        assessment."""
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT revision, list_revision FROM stories "
                    "WHERE project_id = %s AND story_id = %s",
                    (project_id, story_id),
                )
            ).fetchone()
        if row is None:
            raise LookupError(f"unknown story {story_id!r} in project {project_id!r}")
        return row["revision"], row["list_revision"]

    async def state_of(self, project_id: str, story_id: str) -> StoryState:
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT state FROM stories WHERE project_id = %s AND story_id = %s",
                    (project_id, story_id),
                )
            ).fetchone()
        if row is None:
            raise LookupError(f"unknown story {story_id!r} in project {project_id!r}")
        return StoryState(row["state"])


async def add_behaviour_to_story(
    connection: Connection, project_id: str, story_id: str, demonstrates: Sequence[str]
) -> int:
    """Make room for one more behaviour in a story that is being worked on, and return
    its position. Any change to the list makes a new list revision, which the Verifier
    has not vouched for until it is sealed again."""
    story = await _locked_story(connection, project_id, story_id)
    _require_at_work(story)
    if not demonstrates:
        # It could be built and integrated, and then no list containing it could ever
        # be sealed: the story would hold the project and never reach its gate.
        raise ActionRefused("a behaviour demonstrates at least one acceptance criterion")
    unknown = sorted(set(demonstrates) - set(story["criteria"]))
    if unknown:
        raise ActionRefused(f"{story_id} has no acceptance criteria {unknown}")
    listed = await _behaviours_of(connection, project_id, story_id)
    await connection.execute(
        "UPDATE stories SET list_revision = list_revision + 1 "
        "WHERE project_id = %s AND story_id = %s",
        (project_id, story_id),
    )
    return len(listed) + 1


async def require_story_at_work(
    connection: Connection, project_id: str, behaviour: Mapping[str, Any], *, entering_design: bool
) -> None:
    """Refuse to start anything on a behaviour whose story is not being worked on. Called
    with the behaviour's row already locked; the story is locked after it, shared, so the
    story cannot reach its gate while this transaction is deciding."""
    story = await (
        await connection.execute(
            "SELECT * FROM stories WHERE project_id = %s AND story_id = %s FOR SHARE",
            (project_id, behaviour["story_id"]),
        )
    ).fetchone()
    assert story is not None
    _require_at_work(story)
    if entering_design:
        earlier = await (
            await connection.execute(
                "SELECT behaviour_id FROM behaviours WHERE project_id = %s AND story_id = %s "
                "AND position < %s AND state NOT IN ('integrated', 'completed') "
                "ORDER BY position LIMIT 1",
                (project_id, behaviour["story_id"], behaviour["position"]),
            )
        ).fetchone()
        if earlier is not None:
            raise ActionRefused(
                f"{behaviour['behaviour_id']} waits for {earlier['behaviour_id']} to be integrated"
            )


async def evaluate_story_gate(connection: Connection, project_id: str, story_id: str) -> None:
    """Open the story's gate if, and only if, everything it needs is on record: the
    latest list of behaviours is sealed, every one of them is integrated, and the Analyst
    has assessed that list. Run after each event that could complete the set, in that
    event's transaction, so the order in which they happen does not matter."""
    story = await _locked_story(connection, project_id, story_id)
    if not permits_behaviour_work(StoryState(story["state"])) or _is_unsealed(story):
        return
    listed = await _behaviours_of(connection, project_id, story_id)
    assessment = await _assessment(connection, story)
    if assessment is None or any(row["state"] != "integrated" for row in listed):
        return
    await connection.execute(
        "UPDATE stories SET state = 'awaiting_approval' WHERE project_id = %s AND story_id = %s",
        (project_id, story_id),
    )
    manifest = await _story_review(connection, story, listed, assessment)
    await _open_gate(
        connection,
        project_id,
        story["feature_id"],
        kind="story",
        story_id=story_id,
        basis=_story_basis(story, assessment),
        manifest=manifest,
    )


async def release_next(connection: Connection, project_id: str) -> None:
    """Start the one story of this project that may start now, if there is one. Run in
    the transaction of every event that can free the project's place or add a story to
    choose from, so a feature that had to wait is looked at again without a restart."""
    await _locked_project(connection, project_id)
    rows = await (
        await connection.execute(
            "SELECT s.feature_id, s.story_id, s.state FROM stories s "
            "JOIN roadmaps r USING (project_id, feature_id) "
            "JOIN features f USING (project_id, feature_id) "
            "WHERE s.project_id = %s AND r.revision = s.roadmap_revision AND r.state = 'approved' "
            "AND f.state NOT IN ('completed', 'cancelled') AND NOT f.paused "
            "ORDER BY f.opened, s.position",
            (project_id,),
        )
    ).fetchall()
    features: dict[str, list[Candidate]] = {}
    for row in rows:
        features.setdefault(row["feature_id"], []).append(
            Candidate(row["feature_id"], row["story_id"], StoryState(row["state"]))
        )
    chosen = next_story_to_release(list(features.values()))
    if chosen is None:
        return
    await connection.execute(
        "UPDATE stories SET state = 'released' WHERE project_id = %s AND story_id = %s",
        (project_id, chosen.story_id),
    )
    await connection.execute(
        "UPDATE features SET state = 'delivering' WHERE project_id = %s AND feature_id = %s",
        (project_id, chosen.feature_id),
    )
    await append_event(connection, project_id, "story.released", subject_id=chosen.story_id)


def _require_role(principal: Principal, role: str) -> None:
    if principal.kind is not PrincipalKind.WORKER or principal.role != role:
        raise ActionRefused(f"this is for the {role} to do")


def _require_at_work(story: Mapping[str, Any]) -> None:
    if not permits_behaviour_work(StoryState(story["state"])):
        raise ActionRefused(
            f"{story['story_id']} is {story['state'].replace('_', ' ')}, not being worked on"
        )


def _require_revisions(
    story: Mapping[str, Any], story_revision: int, list_revision: int, current_list: int
) -> None:
    if (story_revision, list_revision) != (story["revision"], current_list):
        raise ActionRefused(
            f"{story['story_id']} has moved on: this was about story revision "
            f"{story_revision}, list {list_revision}"
        )


def _is_unsealed(story: Mapping[str, Any]) -> bool:
    return story["sealed_revision"] is None or story["sealed_revision"] != story["list_revision"]


def _story_basis(story: Mapping[str, Any], assessment: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "story_revision": story["revision"],
        "list_revision": story["sealed_revision"],
        "assessment_id": str(assessment["assessment_id"]),
    }


async def _basis_now(
    connection: Connection, gate: Mapping[str, Any], story: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """What a gate of this kind would be opened on if it were opened now."""
    if story is None:
        roadmap = await (
            await connection.execute(
                "SELECT revision FROM roadmaps WHERE project_id = %s AND feature_id = %s "
                "AND state = 'proposed' ORDER BY revision DESC LIMIT 1",
                (gate["project_id"], gate["feature_id"]),
            )
        ).fetchone()
        return {"roadmap_revision": roadmap["revision"]} if roadmap else None
    assessment = await _assessment(connection, story)
    if assessment is None or _is_unsealed(story):
        return None
    return _story_basis(story, assessment)


async def _settle_roadmap(connection: Connection, gate: Mapping[str, Any], outcome: str) -> None:
    await connection.execute(
        "UPDATE roadmaps SET state = %s WHERE project_id = %s AND feature_id = %s "
        "AND revision = %s",
        (
            "approved" if outcome == "approved" else "superseded",
            gate["project_id"],
            gate["feature_id"],
            gate["basis"]["roadmap_revision"],
        ),
    )


async def _settle_story(connection: Connection, story: Mapping[str, Any], outcome: str) -> None:
    if outcome == "approved":
        await connection.execute(
            "UPDATE stories SET state = 'approved' WHERE project_id = %s AND story_id = %s",
            (story["project_id"], story["story_id"]),
        )
        return
    # A new revision of the same story: what was integrated and what was reviewed stay.
    await connection.execute(
        # The seal belonged to the revision that was reviewed; the new one has none.
        "UPDATE stories SET state = 'changes_requested', revision = revision + 1, "
        "sealed_revision = NULL "
        "WHERE project_id = %s AND story_id = %s",
        (story["project_id"], story["story_id"]),
    )


async def _open_gate(
    connection: Connection,
    project_id: str,
    feature_id: str,
    *,
    kind: str,
    story_id: str | None,
    basis: dict[str, Any],
    manifest: dict[str, Any],
) -> uuid.UUID:
    """The gate, its place in the inbox and the notice that it is there, together. The
    notice only says "look": the inbox is the table, and survives the notice being lost."""
    gate_id = uuid.uuid7()
    digest = _digest(manifest)
    await connection.execute(
        "INSERT INTO gates (gate_id, project_id, feature_id, kind, story_id, basis, manifest, "
        "manifest_digest) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (gate_id, project_id, feature_id, kind, story_id, Jsonb(basis), Jsonb(manifest), digest),
    )
    await connection.execute(
        "INSERT INTO inbox_items (item_id, project_id, kind, gate_id) VALUES (%s, %s, 'gate', %s)",
        (uuid.uuid7(), project_id, gate_id),
    )
    opened = await append_event(
        connection,
        project_id,
        f"{kind}.gate_opened",
        subject_id=story_id or feature_id,
        payload={"gate_id": str(gate_id), "manifest_digest": digest, "basis": basis},
    )
    await enqueue(
        connection,
        f"relay:{project_id}:inbox:product-owner",
        Envelope(
            message_kind=MessageKind.EVENT,
            event_type="approval.requested",
            project_id=project_id,
            story_id=story_id,
            sender=CONTROL_PLANE,
            recipient="product-owner",
            idempotency_key=f"gate:{gate_id}",
            correlation_id=uuid.uuid7(),
            causation_id=opened.event_id,
            payload={"gate_id": str(gate_id), "kind": kind},
        ),
    )
    return gate_id


async def _close_gate(
    connection: Connection, gate: Mapping[str, Any], state: str, outcome: str
) -> None:
    await connection.execute(
        "UPDATE gates SET state = %s, outcome = %s WHERE gate_id = %s",
        (state, outcome, gate["gate_id"]),
    )
    await connection.execute(
        "UPDATE inbox_items SET state = 'closed' WHERE gate_id = %s", (gate["gate_id"],)
    )


async def _story_review(
    connection: Connection,
    story: Mapping[str, Any],
    listed: Sequence[Mapping[str, Any]],
    assessment: Mapping[str, Any],
) -> dict[str, Any]:
    """Everything the Product Owner is shown, taken from records and nowhere else."""
    project_id = story["project_id"]
    behaviours = []
    for row in listed:
        evidence = await (
            await connection.execute(
                "SELECT i.candidate_revision, c.red_execution_id::text, c.blocking_from, "
                "c.green_execution_id::text, i.batch_id, i.cycle_id FROM integration_intents i "
                "JOIN review_cycles c USING (cycle_id) WHERE i.project_id = %s "
                "AND i.behaviour_id = %s AND i.state = 'integrated' "
                "ORDER BY i.created_at DESC LIMIT 1",
                (project_id, row["behaviour_id"]),
            )
        ).fetchone()
        assert evidence is not None
        checks = await (
            await connection.execute(
                "SELECT contract_id, execution_id::text FROM check_runs WHERE batch_id = %s "
                "ORDER BY contract_id",
                (evidence["batch_id"],),
            )
        ).fetchall()
        findings = await (
            await connection.execute(
                "SELECT f.finding_id, f.role, f.severity, f.status, f.claim, f.dispute, "
                "f.withdrawal_reason, (v.finding_id IS NOT NULL) AS verified_fixed "
                "FROM findings f LEFT JOIN finding_verifications v "
                "ON v.finding_id = f.finding_id AND v.cycle_id = %s "
                "WHERE f.project_id = %s AND f.behaviour_id = %s ORDER BY f.finding_id",
                (evidence["cycle_id"], project_id, row["behaviour_id"]),
            )
        ).fetchall()
        for finding in findings:
            finding["settled"] = _how_settled(finding, evidence["blocking_from"])
        behaviours.append(
            {
                "behaviour_id": row["behaviour_id"],
                "summary": row["summary"],
                "demonstrates": list(row["demonstrates"]),
                "contract": row["contract"],
                "contract_digest": ProcessContract.from_dict(row["contract"]).digest,
                "integrated_revision": evidence["candidate_revision"],
                "red_execution_id": evidence["red_execution_id"],
                "green_execution_id": evidence["green_execution_id"],
                "checks": checks,
                "findings": findings,
            }
        )
    return {
        "story_id": story["story_id"],
        "story_revision": story["revision"],
        "roadmap_revision": story["roadmap_revision"],
        "title": story["title"],
        "outcome": story["outcome"],
        "criteria": story["criteria"],
        "list_revision": story["sealed_revision"],
        "behaviours": behaviours,
        "assessment": {
            "assessment_id": str(assessment["assessment_id"]),
            "statement": assessment["statement"],
        },
        # Answers the Product Owner gave to questions about this story. None can exist
        # yet; the gate decisions themselves are in the ledger, not here.
        "question_answers": [],
        # Not measured yet. Unknown is not zero, and is never shown as zero.
        "usage": "unknown",
    }


def _how_settled(finding: Mapping[str, Any], blocking_from: int) -> str:
    """Why this finding did not stop the candidate that is now on trunk."""
    if finding["status"] == "withdrawn":
        return f"withdrawn by {finding['role']}: {finding['withdrawal_reason']}"
    if finding["verified_fixed"]:
        return f"{finding['role']} verified the fix on the integrated candidate"
    if finding["severity"] < blocking_from:
        return "below the severity that blocks; left open"
    return "NOT SETTLED: a blocking finding with no verification on the integrated candidate"


def _digest(manifest: dict[str, Any]) -> str:
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


async def _recorded(connection: Connection, action: HumanAction) -> tuple[str, Receipt] | None:
    row = await (
        await connection.execute(
            "SELECT digest, disposition, outcome FROM human_actions "
            "WHERE principal = %s AND request_id = %s",
            (action.principal.identity, action.request_id),
        )
    ).fetchone()
    return (row["digest"], Receipt(row["disposition"], row["outcome"])) if row else None


async def _record(connection: Connection, action: HumanAction, receipt: Receipt) -> None:
    """Store the receipt. The key decides: if the same principal and request id were
    recorded meanwhile, by a request in another project say, this one is refused and
    everything it did is undone with it."""
    stored = await (
        await connection.execute(
            "INSERT INTO human_actions (principal, request_id, target, action, payload, digest, "
            "disposition, outcome) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (principal, request_id) DO NOTHING RETURNING request_id",
            (
                action.principal.identity,
                action.request_id,
                action.target,
                action.action,
                Jsonb(dict(action.payload)),
                action.digest,
                receipt.disposition,
                receipt.outcome,
            ),
        )
    ).fetchone()
    if stored is None:
        raise ActionRefused(
            f"request id {action.request_id!r} was already used for a different action"
        )


async def lock_story(connection: Connection, project_id: str, story_id: str) -> None:
    await _locked_story(connection, project_id, story_id)


async def _locked_story(connection: Connection, project_id: str, story_id: str) -> dict[str, Any]:
    row = await (
        await connection.execute(
            "SELECT * FROM stories WHERE project_id = %s AND story_id = %s FOR UPDATE",
            (project_id, story_id),
        )
    ).fetchone()
    if row is None:
        raise LookupError(f"unknown story {story_id!r} in project {project_id!r}")
    return row


async def _locked_feature(
    connection: Connection, project_id: str, feature_id: str
) -> dict[str, Any]:
    row = await (
        await connection.execute(
            "SELECT * FROM features WHERE project_id = %s AND feature_id = %s FOR UPDATE",
            (project_id, feature_id),
        )
    ).fetchone()
    if row is None:
        raise LookupError(f"unknown feature {feature_id!r} in project {project_id!r}")
    return row


async def _locked_project(connection: Connection, project_id: str) -> None:
    await connection.execute(
        "SELECT 1 FROM projects WHERE project_id = %s FOR NO KEY UPDATE", (project_id,)
    )


async def _behaviours_of(
    connection: Connection, project_id: str, story_id: str
) -> list[dict[str, Any]]:
    return await (
        await connection.execute(
            "SELECT behaviour_id, summary, state, contract, demonstrates FROM behaviours "
            "WHERE project_id = %s AND story_id = %s ORDER BY position",
            (project_id, story_id),
        )
    ).fetchall()


async def _assessment(connection: Connection, story: Mapping[str, Any]) -> dict[str, Any] | None:
    return await (
        await connection.execute(
            "SELECT assessment_id, statement FROM story_assessments WHERE project_id = %s "
            "AND story_id = %s AND story_revision = %s AND list_revision = %s",
            (story["project_id"], story["story_id"], story["revision"], story["sealed_revision"]),
        )
    ).fetchone()
