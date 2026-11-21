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
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from psycopg.types.json import Jsonb

from relay.application.assignments import (
    CORRECT,
    owe,
    retire_all_of_feature,
    retire_all_of_story,
    retire_what_a_roadmap_answers,
    revoke_unstarted_grants,
)
from relay.application.closure import (
    CANCELLED,
    PAUSED,
    as_shown,
    closure_of_feature,
    closure_of_story,
    decisions_in_force,
    feature_basis,
    lock_gate,
    require_basis,
    require_open,
    require_startable,
    story_basis,
)
from relay.domain.evidence import ProcessContract
from relay.domain.identifiers import require_identifier
from relay.domain.story import (
    HOLDING_THE_SLOT,
    ON_A_FEATURE,
    ON_A_STORY,
    ActionRefused,
    Candidate,
    Entry,
    HumanAction,
    Member,
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
    why_not_a_revision,
)
from relay.persistence.database import Connection, Pool
from relay.persistence.ledger import append_event
from relay.persistence.outbox import enqueue
from relay.transport.envelope import Envelope, MessageKind

CONTROL_PLANE = "relay/control-plane"


@dataclass(frozen=True)
class StoryDraft:
    """A story as proposed: a new one, which may be the correction of a rejected story."""

    story_id: str
    title: str
    outcome: str
    criteria: Mapping[str, str]
    corrects: str | None = None

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
class Kept:
    """A story of the roadmap being revised, as it is, at the place this entry holds."""

    story_id: str


@dataclass(frozen=True)
class Reworded:
    """A planned story given a new text. It names the story's revision and decision
    basis as the Analyst read them: if either has moved by approval, this is no longer
    an edit of that story."""

    story_id: str
    title: str
    outcome: str
    criteria: Mapping[str, str]
    story_revision: int
    story_basis: int

    def __post_init__(self) -> None:
        text = StoryDraft(self.story_id, self.title, self.outcome, self.criteria)
        object.__setattr__(self, "criteria", text.criteria)


@dataclass(frozen=True)
class Dropped:
    """A planned story left out, with the revision and basis it was read at."""

    story_id: str
    story_revision: int
    story_basis: int


Proposed = StoryDraft | Kept | Reworded | Dropped


@dataclass(frozen=True)
class Roadmap:
    """Where a feature's roadmap stands: the revision in force, the proposal waiting, and
    the stories in force, in order."""

    effective: int | None
    proposed: int | None
    stories: tuple[str, ...]


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
            await lock_gate(connection, project_id)
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
        self,
        principal: Principal,
        project_id: str,
        feature_id: str,
        entries: Sequence[Proposed],
        *,
        basis: int,
        revises: int | None = None,
        replaces: int | None = None,
    ) -> uuid.UUID:
        """The Analyst's proposal for a feature's roadmap: its first, or a revision of
        the one in force. It names the decision basis it was written on, the revision
        it revises and the waiting proposal it replaces, each `None` if there is none;
        all three are compared under the lock. It is stored whole and changes nothing
        until a person approves it at the gate this opens."""
        _require_role(principal, "analyst")
        # Read once, before anything is awaited. What is stored and what is shown are
        # both made from this, so they cannot differ.
        entries = tuple(entries)
        if not entries:
            raise ValueError("a roadmap needs at least one story")
        async with self._pool.connection() as connection, connection.transaction():
            # Exclusively: a proposal inspects, and its approval changes, stories that
            # other transactions lock in their own order under the shared gate lock.
            await lock_gate(connection, project_id, exclusive=True)
            await _locked_project(connection, project_id)
            feature = await _locked_feature(connection, project_id, feature_id)
            require_open(await closure_of_feature(connection, project_id, feature_id))
            require_basis(basis, await feature_basis(connection, project_id, feature_id))
            effective, proposed = await _revisions_of(connection, project_id, feature_id)
            if revises != effective:
                raise ActionRefused(
                    f"this was written against roadmap revision {revises or 'none'}; "
                    f"the one in force is {effective or 'none'}: read it again"
                )
            if replaces != proposed:
                raise ActionRefused(
                    f"proposal {proposed} is waiting: a new one has to say it replaces it"
                    if proposed is not None
                    else f"there is no proposal {replaces} waiting to be replaced"
                )
            why = why_not_a_revision(
                await _members(connection, project_id, feature_id, effective),
                [_as_entry(entry) for entry in entries],
            ) or await _why_an_edit_is_out_of_date(
                connection, project_id, [_as_row(entry, 0) for entry in entries]
            )
            if why:
                raise ActionRefused(why)
            await _require_unused(
                connection, project_id, [e.story_id for e in entries if isinstance(e, StoryDraft)]
            )
            if proposed is not None:
                await _end_proposal(connection, project_id, feature_id, proposed, "replaced")
            row = await (
                await connection.execute(
                    "SELECT coalesce(max(revision), 0) + 1 AS next FROM roadmaps "
                    "WHERE project_id = %s AND feature_id = %s",
                    (project_id, feature_id),
                )
            ).fetchone()
            assert row is not None
            revision: int = row["next"]
            await connection.execute(
                "INSERT INTO roadmaps (project_id, feature_id, revision, current_on) "
                "VALUES (%s, %s, %s, %s)",
                (project_id, feature_id, revision, basis),
            )
            position = 0
            for entry in entries:
                position += 0 if isinstance(entry, Dropped) else 1
                stored = _as_row(entry, position)
                await connection.execute(
                    "INSERT INTO roadmap_entries (project_id, feature_id, revision, story_id, "
                    "position, kind, title, outcome, criteria, corrects, story_revision, "
                    "story_basis) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        project_id,
                        feature_id,
                        revision,
                        stored["story_id"],
                        stored["position"],
                        stored["kind"],
                        stored["title"],
                        stored["outcome"],
                        Jsonb(stored["criteria"]) if stored["criteria"] is not None else None,
                        stored["corrects"],
                        stored["story_revision"],
                        stored["story_basis"],
                    ),
                )
            return await _open_roadmap_gate(connection, project_id, feature, revision, basis)

    async def seal(
        self,
        principal: Principal,
        project_id: str,
        story_id: str,
        *,
        story_revision: int,
        list_revision: int,
        basis: int,
    ) -> uuid.UUID:
        """The Verifier vouches that the behaviours of one named list revision, and no
        others, show the story's criteria, under the decisions of one named basis. A seal
        that arrives after the list or the decisions have moved on vouches for something
        that is no longer there, and is refused. Returns the seal's identity, which an
        assessment has to name."""
        _require_role(principal, "verifier")
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
            story = await _locked_story(connection, project_id, story_id)
            _require_at_work(story)
            listed = await _behaviours_of(connection, project_id, story_id)
            await _require_all_startable(connection, project_id, story_id, listed)
            _require_revisions(story, story_revision, list_revision, story["list_revision"])
            require_basis(basis, await story_basis(connection, project_id, story_id))
            require_sealable(
                story["criteria"].keys(),
                {row["behaviour_id"]: row["demonstrates"] for row in listed},
            )
            seal_id = uuid.uuid7()
            await connection.execute(
                "UPDATE stories SET sealed_revision = list_revision, seal_id = %s "
                "WHERE project_id = %s AND story_id = %s",
                (seal_id, project_id, story_id),
            )
            await append_event(
                connection,
                project_id,
                "story.behaviours_sealed",
                subject_id=story_id,
                payload={
                    "seal_id": str(seal_id),
                    "list_revision": story["list_revision"],
                    "behaviours": [row["behaviour_id"] for row in listed],
                },
            )
            await evaluate_story_gate(connection, project_id, story_id)
            return seal_id

    async def assess(
        self,
        principal: Principal,
        project_id: str,
        story_id: str,
        statement: str,
        *,
        story_revision: int,
        list_revision: int,
        basis: int,
        seal: uuid.UUID,
    ) -> None:
        """The Analyst says whether one named sealed list demonstrates the outcome. The
        statement is shown to the Product Owner as written, so it must be about the seal
        and the decisions the Analyst read, and is refused if either has moved on."""
        _require_role(principal, "analyst")
        if not statement.strip():
            raise ValueError("an assessment says something")
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
            story = await _locked_story(connection, project_id, story_id)
            _require_at_work(story)
            await _require_all_startable(
                connection,
                project_id,
                story_id,
                await _behaviours_of(connection, project_id, story_id),
            )
            if _is_unsealed(story):
                raise ActionRefused("only a sealed list of behaviours can be assessed")
            _require_revisions(story, story_revision, list_revision, story["sealed_revision"])
            require_basis(basis, await story_basis(connection, project_id, story_id))
            if seal != story["seal_id"]:
                raise ActionRefused("this assessment is of a seal that is no longer the story's")
            if await _assessment(connection, story) is not None:
                raise ActionRefused("this sealed list has already been assessed")
            await connection.execute(
                "INSERT INTO story_assessments (assessment_id, project_id, story_id, "
                "story_revision, list_revision, statement, seal_id, basis) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    uuid.uuid7(),
                    project_id,
                    story_id,
                    story["revision"],
                    story["sealed_revision"],
                    statement,
                    seal,
                    basis,
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
            # A roadmap gate is decided under the exclusive gate lock, as its proposal
            # was published: approving it reads and rewrites other stories.
            await lock_gate(connection, project_id, exclusive=located["kind"] != "story")
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
            stopped = await closure_of_feature(
                connection, project_id, located["feature_id"], located["story_id"]
            )
            if gate["state"] == "open" and stopped is not None and stopped != CANCELLED:
                # Nothing is decided while the feature is paused or a question covers
                # the gate, and nothing is recorded: the gate is still there to be
                # decided afterwards. A gate that is no longer open is another matter:
                # it will never be decided, and saying it waits would be untrue.
                raise ActionRefused(f"the feature is {stopped}: its gates wait")
            if gate["kind"] == "supersession":
                from relay.application import supersessions  # it imports this module

                current = await supersessions.basis_of_gate(connection, gate)
                gate = await (
                    await connection.execute("SELECT * FROM gates WHERE gate_id = %s", (gate_id,))
                ).fetchone()
                assert gate is not None
            else:
                current = await _basis_now(connection, gate, story)
            if gate["state"] != "open" or gate["basis"] != current:
                if gate["state"] == "open":
                    # What this gate showed is no longer what there is to decide on.
                    await _close_gate(connection, gate, "superseded", "superseded")
                receipt = Receipt("stale", "nothing changed: this gate is no longer open")
                await _record(connection, action, receipt)
                return receipt
            if gate["kind"] != "story" and action.action == "reject":
                raise ActionRefused(
                    "'reject' is not something that can be done here: a roadmap is sent "
                    "back with request_changes"
                )
            outcome = {"approve": "approved", "reject": "rejected"}.get(
                action.action, "changes_requested"
            )
            moved_on = (
                await _settle_roadmap(connection, gate, outcome)
                if gate["kind"] == "roadmap"
                else ""
            )
            if moved_on:
                # What this proposal is about has changed since it was written. It is
                # not approved as it stands; the Analyst proposes again.
                await _close_gate(connection, gate, "superseded", "superseded")
                receipt = Receipt("stale", f"nothing changed: {moved_on}")
                await _record(connection, action, receipt)
                return receipt
            await _close_gate(connection, gate, "decided", outcome)
            if gate["kind"] == "story":
                assert story is not None
                await _settle_story(connection, story, outcome)
            elif gate["kind"] == "supersession":
                await supersessions.settle_gate(connection, gate, outcome)
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

    async def reject(self, project_id: str, action: HumanAction) -> Receipt:
        """The Product Owner sets aside a story that is being worked on, for instance
        because an answer has changed what it should do. The action names the story's
        revision and decision basis as the person saw them: if either has moved, or the
        story is no longer being worked on, nothing changes and the receipt says so."""
        require_human(action.principal)
        require_well_formed(action, ON_A_STORY)
        story_id = action.target
        try:
            revision, basis = int(action.payload["revision"]), int(action.payload["basis"])
        except ValueError:
            raise ActionRefused("reject names a revision and a basis, as numbers") from None
        # A story's name is its project's own: the receipt is for this project's story.
        action = replace(action, target=f"{project_id}/{story_id}")
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id, exclusive=True)
            story = await _locked_story(connection, project_id, story_id)
            await _locked_project(connection, project_id)
            if (seen := answer_again(action, await _recorded(connection, action))) is not None:
                return seen
            as_seen = (revision, basis) == (
                story["revision"],
                await story_basis(connection, project_id, story_id),
            )
            if not as_seen or not permits_behaviour_work(StoryState(story["state"])):
                receipt = Receipt(
                    "stale",
                    f"nothing changed: {story_id} is {story['state'].replace('_', ' ')} at "
                    f"revision {story['revision']}, not what this was about",
                )
                await _record(connection, action, receipt)
                return receipt
            await _reject(connection, story)
            await append_event(
                connection,
                project_id,
                "story.rejected",
                subject_id=story_id,
                payload={
                    "by": action.principal.identity,
                    "request_id": action.request_id,
                    "revision": revision,
                    "said": {"reason": action.payload["reason"]},
                },
            )
            await release_next(connection, project_id)
            receipt = Receipt("effective", "rejected")
            await _record(connection, action, receipt)
            return receipt

    async def roadmap_of(self, project_id: str, feature_id: str) -> Roadmap:
        """Where the feature's roadmap stands, as a role reads it before proposing a
        revision: what it reads here is what its proposal has to name."""
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
            effective, proposed = await _revisions_of(connection, project_id, feature_id)
            members = await _members(connection, project_id, feature_id, effective)
        return Roadmap(effective, proposed, tuple(member.story_id for member in members))

    async def seal_of(self, project_id: str, story_id: str) -> uuid.UUID | None:
        """The story's current seal, which an assessment names."""
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT seal_id FROM stories WHERE project_id = %s AND story_id = %s",
                    (project_id, story_id),
                )
            ).fetchone()
        if row is None:
            raise LookupError(f"unknown story {story_id!r} in project {project_id!r}")
        seal: uuid.UUID | None = row["seal_id"]
        return seal

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
    connection: Connection,
    project_id: str,
    story_id: str,
    demonstrates: Sequence[str],
    basis: int,
    story_revision: int,
) -> int:
    """Make room for one more behaviour in a story that is being worked on, and return
    its position. Any change to the list makes a new list revision, which the Verifier
    has not vouched for until it is sealed again."""
    story = await _locked_story(connection, project_id, story_id)
    _require_at_work(story)
    await require_startable(connection, project_id, story_id)
    require_basis(basis, await story_basis(connection, project_id, story_id))
    if story_revision != story["revision"]:
        raise ActionRefused(
            f"{story_id} has moved on: this was proposed against revision {story_revision}, "
            f"and it is now at revision {story['revision']}"
        )
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
    connection: Connection,
    project_id: str,
    behaviour: Mapping[str, Any],
    *,
    entering_design: bool,
    on_its_contract: bool = False,
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
    # Work that runs, reviews or integrates the behaviour relies on its contract and the
    # evidence gathered so far; work before that relies on the story alone.
    await require_startable(
        connection,
        project_id,
        behaviour["story_id"],
        behaviour["behaviour_id"] if on_its_contract else None,
    )
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
    if await closure_of_story(connection, project_id, story_id) is not None:
        return  # a closed feature opens no gate; resume looks again
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
        basis=_story_basis(story, assessment, await story_basis(connection, project_id, story_id)),
        manifest=manifest,
    )


async def close_feature(pool: Pool, project_id: str, action: HumanAction) -> Receipt:
    """Pause, resume or cancel the feature the action names: the transaction only. The
    entry point is `Features.close`, which also does what a resume leaves owed. The transaction holds
    the project's gate lock exclusively, so by the time it commits no transaction
    that authorises work is in flight, and every later one sees the closure."""
    require_human(action.principal)
    require_well_formed(action, ON_A_FEATURE)
    feature_id = action.target
    # A feature's id is its project's own. The receipt is for this project's feature, so
    # the same request sent to another project is a different action, not a replay.
    action = replace(action, target=f"{project_id}/{feature_id}")
    async with pool.connection() as connection, connection.transaction():
        await lock_gate(connection, project_id, exclusive=True)
        await _locked_project(connection, project_id)
        feature = await _locked_feature(connection, project_id, feature_id)
        if (seen := answer_again(action, await _recorded(connection, action))) is not None:
            return seen
        closed_by = feature["closed_by"]
        pointless = (action.action == "pause" and closed_by == PAUSED) or (
            action.action == "resume" and closed_by != PAUSED
        )
        if closed_by == CANCELLED or pointless:
            # Cancelled for good; or pausing what is paused; or resuming what is not.
            # A paused feature can still be cancelled.
            receipt = Receipt("stale", f"nothing changed: the feature is {closed_by or 'open'}")
            await _record(connection, action, receipt)
            return receipt
        if action.action == "cancel":
            await _cancel(connection, project_id, feature_id)
            await retire_all_of_feature(connection, project_id, feature_id)
        if action.action == "pause":
            await revoke_unstarted_grants(connection, project_id, feature_id)
        await connection.execute(
            "UPDATE features SET closed_by = %s, state = %s "
            "WHERE project_id = %s AND feature_id = %s",
            (
                {"pause": PAUSED, "cancel": CANCELLED, "resume": None}[action.action],
                CANCELLED if action.action == "cancel" else feature["state"],
                project_id,
                feature_id,
            ),
        )
        outcome = {"pause": "paused", "cancel": "cancelled", "resume": "resumed"}[action.action]
        await append_event(
            connection,
            project_id,
            f"feature.{outcome}",
            subject_id=feature_id,
            payload={
                "by": action.principal.identity,
                "request_id": action.request_id,
                "said": dict(action.payload),
            },
        )
        if action.action == "resume":
            # Whatever became ready while the feature was closed is owed now, with
            # no event left to cause it.
            working = await (
                await connection.execute(
                    "SELECT story_id FROM stories WHERE project_id = %s AND feature_id = %s "
                    "AND state IN ('released', 'changes_requested') ORDER BY story_id",
                    (project_id, feature_id),
                )
            ).fetchall()
            for story in working:
                await evaluate_story_gate(connection, project_id, story["story_id"])
        await release_next(connection, project_id)
        receipt = Receipt("effective", outcome)
        await _record(connection, action, receipt)
        return receipt


async def reopen_roadmap_gate(connection: Connection, project_id: str, feature_id: str) -> None:
    """A roadmap still waiting for approval was reconfirmed after a decision: put it to
    the Product Owner again, exactly as it was proposed, with the decisions now in force."""
    _, proposed = await _revisions_of(connection, project_id, feature_id)
    if proposed is None:
        return  # approved already: the Product Owner has seen it and made the decision
    feature = await _locked_feature(connection, project_id, feature_id)
    await _open_roadmap_gate(
        connection,
        project_id,
        feature,
        proposed,
        await feature_basis(connection, project_id, feature_id),
    )


async def _open_roadmap_gate(
    connection: Connection,
    project_id: str,
    feature: Mapping[str, Any],
    revision: int,
    basis: int,
) -> uuid.UUID:
    """Open the gate on a stored proposal. The review is made from what is stored: the
    resulting stories in order, and what changes against the roadmap in force."""
    feature_id = feature["feature_id"]
    entries = await _entries(connection, project_id, feature_id, revision)
    live = {
        row["story_id"]: row
        for row in await (
            await connection.execute(
                "SELECT story_id, title, outcome, criteria, corrects FROM stories "
                "WHERE project_id = %s AND story_id = ANY(%s)",
                (project_id, [entry["story_id"] for entry in entries]),
            )
        ).fetchall()
    }

    def text(source: Mapping[str, Any]) -> dict[str, Any]:
        return {key: source[key] for key in ("title", "outcome", "criteria")}

    stories = []
    for entry in entries:
        if entry["kind"] == "dropped":
            continue
        shown = {"story_id": entry["story_id"]} | text(
            live[entry["story_id"]] if entry["kind"] == "kept" else entry
        )
        # A correction keeps saying what it corrects, in every later revision.
        # A new story corrects what its proposal says, and nothing else: a story of the
        # same name may meanwhile have been created by another feature.
        corrects = (
            entry["corrects"]
            if entry["kind"] == "new"
            else live.get(entry["story_id"], {}).get("corrects")
        )
        if corrects is not None:
            shown["corrects"] = corrects
        stories.append(shown)
    effective, _ = await _revisions_of(connection, project_id, feature_id)
    was_at = {
        member.story_id: place
        for place, member in enumerate(
            await _members(connection, project_id, feature_id, effective), start=1
        )
    }
    manifest = {
        "feature_id": feature_id,
        "problem": feature["problem"],
        "roadmap_revision": revision,
        "revises": effective,
        "decisions": as_shown(await decisions_in_force(connection, project_id, feature_id)),
        "stories": stories,
        "changes": {
            "kept": [e["story_id"] for e in entries if e["kind"] == "kept"],
            "reworded": [
                {"story_id": e["story_id"], "from": text(live[e["story_id"]]), "to": text(e)}
                for e in entries
                if e["kind"] == "reworded"
            ],
            "dropped": [e["story_id"] for e in entries if e["kind"] == "dropped"],
            "added": [e["story_id"] for e in entries if e["kind"] == "new"],
            # A reorder changes nothing else, and would otherwise show as nothing at all.
            "moved": [
                {"story_id": e["story_id"], "from": was_at[e["story_id"]], "to": e["position"]}
                for e in entries
                if e["kind"] in ("kept", "reworded") and was_at[e["story_id"]] != e["position"]
            ],
        },
    }
    return await _open_gate(
        connection,
        project_id,
        feature_id,
        kind="roadmap",
        story_id=None,
        basis={"roadmap_revision": revision, "decisions": basis},
        manifest=manifest,
    )


async def _revisions_of(
    connection: Connection, project_id: str, feature_id: str
) -> tuple[int | None, int | None]:
    """The revision in force and the proposal waiting, either of which may be absent."""
    rows = await (
        await connection.execute(
            "SELECT revision, state FROM roadmaps WHERE project_id = %s AND feature_id = %s "
            "AND state IN ('approved', 'proposed')",
            (project_id, feature_id),
        )
    ).fetchall()
    by_state = {row["state"]: row["revision"] for row in rows}
    return by_state.get("approved"), by_state.get("proposed")


async def _entries(
    connection: Connection, project_id: str, feature_id: str, revision: int
) -> list[dict[str, Any]]:
    return await (
        await connection.execute(
            "SELECT * FROM roadmap_entries WHERE project_id = %s AND feature_id = %s "
            "AND revision = %s ORDER BY position NULLS LAST, story_id",
            (project_id, feature_id, revision),
        )
    ).fetchall()


async def _members(
    connection: Connection, project_id: str, feature_id: str, revision: int | None
) -> list[Member]:
    """The stories of a revision in its order, as they stand now."""
    if revision is None:
        return []
    rows = await (
        await connection.execute(
            "SELECT s.story_id, s.state, s.corrects FROM roadmap_entries e "
            "JOIN stories s ON s.project_id = e.project_id AND s.story_id = e.story_id "
            "WHERE e.project_id = %s AND e.feature_id = %s AND e.revision = %s "
            "AND e.kind <> 'dropped' ORDER BY e.position",
            (project_id, feature_id, revision),
        )
    ).fetchall()
    return [Member(row["story_id"], StoryState(row["state"]), row["corrects"]) for row in rows]


def _as_entry(proposed: Proposed) -> Entry:
    if isinstance(proposed, StoryDraft):
        return Entry(proposed.story_id, "new", proposed.corrects)
    kind = {Kept: "kept", Reworded: "reworded", Dropped: "dropped"}[type(proposed)]
    return Entry(proposed.story_id, kind)


def _as_row(proposed: Proposed, position: int) -> dict[str, Any]:
    row: dict[str, Any] = {
        "story_id": proposed.story_id,
        "kind": _as_entry(proposed).kind,
        "position": None if isinstance(proposed, Dropped) else position,
        "title": None,
        "outcome": None,
        "criteria": None,
        "corrects": None,
        "story_revision": None,
        "story_basis": None,
    }
    if isinstance(proposed, StoryDraft | Reworded):
        row |= {
            "title": proposed.title,
            "outcome": proposed.outcome,
            "criteria": dict(proposed.criteria),
        }
    if isinstance(proposed, StoryDraft):
        row["corrects"] = proposed.corrects
    if isinstance(proposed, Reworded | Dropped):
        row |= {"story_revision": proposed.story_revision, "story_basis": proposed.story_basis}
    return row


async def _why_an_edit_is_out_of_date(
    connection: Connection, project_id: str, entries: Sequence[Mapping[str, Any]]
) -> str:
    """An edit is of one story at one revision under one set of decisions. Why any of
    these is no longer that, or an empty string."""
    for entry in entries:
        if entry["kind"] not in ("reworded", "dropped"):
            continue
        story = await (
            await connection.execute(
                "SELECT revision, story_basis(project_id, story_id) AS basis, "
                "standing(project_id, feature_id, story_id) AS standing FROM stories "
                "WHERE project_id = %s AND story_id = %s FOR UPDATE",
                (project_id, entry["story_id"]),
            )
        ).fetchone()
        assert story is not None
        if (story["revision"], story["basis"]) != (entry["story_revision"], entry["story_basis"]):
            return (
                f"{entry['story_id']} has moved on: this change was written against revision "
                f"{entry['story_revision']} on decision basis {entry['story_basis']}"
            )
        if story["standing"] is not None:
            return f"{entry['story_id']} cannot be changed while the feature is {story['standing']}"
    return ""


async def _require_unused(connection: Connection, project_id: str, story_ids: list[str]) -> None:
    if why := await _why_a_name_is_taken(connection, project_id, story_ids):
        raise ActionRefused(why)


async def _why_a_name_is_taken(
    connection: Connection, project_id: str, story_ids: list[str]
) -> str:
    taken = await (
        await connection.execute(
            "SELECT story_id FROM stories WHERE project_id = %s AND story_id = ANY(%s) "
            "ORDER BY story_id LIMIT 1",
            (project_id, story_ids),
        )
    ).fetchone()
    if taken is None:
        return ""
    return f"{taken['story_id']} is already the name of a story of this project"


async def _end_proposal(
    connection: Connection, project_id: str, feature_id: str, revision: int, state: str
) -> None:
    """A proposal that will not be approved: sent back at its gate, or replaced. Whatever
    gate is open on it closes with it."""
    await connection.execute(
        "UPDATE roadmaps SET state = %s WHERE project_id = %s AND feature_id = %s "
        "AND revision = %s",
        (state, project_id, feature_id, revision),
    )
    gates = await (
        await connection.execute(
            "SELECT * FROM gates WHERE project_id = %s AND feature_id = %s AND kind = 'roadmap' "
            "AND state = 'open' FOR UPDATE",
            (project_id, feature_id),
        )
    ).fetchall()
    for gate in gates:
        await _close_gate(connection, gate, "superseded", "superseded")
    await retire_what_a_roadmap_answers(connection, project_id, feature_id, revisions=[revision])


async def why_a_proposal_cannot_stand(
    connection: Connection, project_id: str, feature_id: str, revision: int
) -> str:
    """Why a waiting proposal cannot simply be reconfirmed after a decision, or an empty
    string: one of its edits was written about a story on a basis that has since moved."""
    return await _why_an_edit_is_out_of_date(
        connection, project_id, await _entries(connection, project_id, feature_id, revision)
    )


async def revisions_of_roadmap(
    connection: Connection, project_id: str, feature_id: str
) -> tuple[int | None, int | None]:
    return await _revisions_of(connection, project_id, feature_id)


async def release_next(connection: Connection, project_id: str) -> None:
    """Start the one story of this project that may start now, if there is one. Run in
    the transaction of every event that can free the project's place or add a story to
    choose from, so a feature that had to wait is looked at again without a restart."""
    await _locked_project(connection, project_id)
    # The place is counted across every feature of the project, a paused one included,
    # before any feature is left out of the choice.
    holding = await (
        await connection.execute(
            "SELECT 1 FROM stories WHERE project_id = %s AND state = ANY(%s) LIMIT 1",
            (project_id, [state.value for state in HOLDING_THE_SLOT]),
        )
    ).fetchone()
    rows = await (
        await connection.execute(
            "SELECT s.feature_id, s.story_id, s.state, s.corrects, "
            "(standing(s.project_id, s.feature_id, s.story_id) IS NOT NULL "
            "OR r.current_on <> feature_basis(s.project_id, s.feature_id) "
            "OR not_current(s.project_id, s.story_id, NULL) IS NOT NULL) AS stopped "
            "FROM roadmaps r JOIN roadmap_entries e USING (project_id, feature_id, revision) "
            "JOIN stories s ON s.project_id = e.project_id AND s.story_id = e.story_id "
            "JOIN features f ON f.project_id = r.project_id AND f.feature_id = r.feature_id "
            "WHERE r.project_id = %s AND r.state = 'approved' AND e.kind <> 'dropped' "
            "AND f.state NOT IN ('completed', 'cancelled') "
            "ORDER BY f.opened, e.position",
            (project_id,),
        )
    ).fetchall()
    features: dict[str, list[Candidate]] = {}
    for row in rows:
        features.setdefault(row["feature_id"], []).append(
            Candidate(
                row["feature_id"],
                row["story_id"],
                StoryState(row["state"]),
                row["stopped"],
                row["corrects"],
            )
        )
    chosen = next_story_to_release(list(features.values()), slot_taken=holding is not None)
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


async def _cancel(connection: Connection, project_id: str, feature_id: str) -> None:
    """Free the project's place and withdraw everything still waiting for a person."""
    await connection.execute(
        "UPDATE stories SET state = 'cancelled' WHERE project_id = %s AND feature_id = %s "
        "AND state NOT IN ('approved', 'rejected', 'withdrawn')",
        (project_id, feature_id),
    )
    open_gates = await (
        await connection.execute(
            "SELECT * FROM gates WHERE project_id = %s AND feature_id = %s AND state = 'open' "
            "FOR UPDATE",
            (project_id, feature_id),
        )
    ).fetchall()
    for gate in open_gates:
        await _close_gate(connection, gate, "superseded", "superseded")
    # Nobody is left to answer for: the questions go, with their place in the inbox.
    await connection.execute(
        "UPDATE inbox_items SET state = 'closed' WHERE state = 'open' AND question_id IN "
        "(SELECT question_id FROM questions WHERE project_id = %s AND feature_id = %s "
        "AND state = 'open')",
        (project_id, feature_id),
    )
    await connection.execute(
        "UPDATE questions SET state = 'withdrawn' WHERE project_id = %s AND feature_id = %s "
        "AND state = 'open'",
        (project_id, feature_id),
    )
    # Checks a pause was holding, or that were waiting to be run again, never will be
    # now. A request that asks about them is told so, not that they are still waiting.
    await connection.execute(
        "UPDATE check_batches k SET state = 'abandoned', "
        "reason = 'the feature was cancelled before these checks could be run again' "
        "WHERE k.project_id = %s AND (k.state = 'held' OR (k.state = 'interrupted' AND EXISTS "
        "(SELECT 1 FROM assignments a WHERE a.kind = 'integrate' AND a.state = 'wanted' "
        "AND a.issued_on->>'batch_id' = k.batch_id::text))) AND k.behaviour_id IN "
        "(SELECT b.behaviour_id FROM behaviours b JOIN stories s USING (project_id, story_id) "
        "WHERE b.project_id = %s AND s.feature_id = %s)",
        (project_id, project_id, feature_id),
    )


async def _require_all_startable(
    connection: Connection, project_id: str, story_id: str, listed: Sequence[Mapping[str, Any]]
) -> None:
    """A seal and an assessment vouch for the story and for every behaviour in its list.
    Refused once, with every reason: the closure, the story, each stale contract."""
    stopped = await closure_of_story(connection, project_id, story_id)
    if stopped == CANCELLED:
        require_open(stopped)
    reasons = [f"the feature is {stopped}"] if stopped else []
    for behaviour in listed or [{"behaviour_id": None}]:
        row = await (
            await connection.execute(
                "SELECT not_current(%s, %s, %s) AS why",
                (project_id, story_id, behaviour["behaviour_id"]),
            )
        ).fetchone()
        assert row is not None
        for why in (row["why"] or "").split("; and "):
            if why and why not in reasons:
                reasons.append(why)
    if reasons:
        raise ActionRefused(
            "; and ".join(reasons) + ": nothing is vouched for until each is cleared"
        )


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
    return story["seal_id"] is None or story["sealed_revision"] != story["list_revision"]


def _story_basis(
    story: Mapping[str, Any], assessment: Mapping[str, Any], decisions: int
) -> dict[str, Any]:
    return {
        "story_revision": story["revision"],
        "list_revision": story["sealed_revision"],
        "assessment_id": str(assessment["assessment_id"]),
        # A decision of the Product Owner's over the story makes the gate stale.
        "decisions": decisions,
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
        if roadmap is None:
            return None
        return {
            "roadmap_revision": roadmap["revision"],
            "decisions": await feature_basis(connection, gate["project_id"], gate["feature_id"]),
        }
    assessment = await _assessment(connection, story)
    if assessment is None or _is_unsealed(story):
        return None
    return _story_basis(
        story, assessment, await story_basis(connection, story["project_id"], story["story_id"])
    )


async def _settle_roadmap(connection: Connection, gate: Mapping[str, Any], outcome: str) -> str:
    """Apply the person's decision on a proposal. Sent back, it ends. Approved, it
    replaces the roadmap in force, all at once: but only if it is still a valid change
    to what is there now, and each of its edits is still of the story it was written
    against. Returns why not, having changed nothing, if it is not; otherwise an
    empty string."""
    project_id, feature_id = gate["project_id"], gate["feature_id"]
    revision: int = gate["basis"]["roadmap_revision"]
    if outcome != "approved":
        await connection.execute(
            "UPDATE roadmaps SET state = 'sent_back' WHERE project_id = %s AND feature_id = %s "
            "AND revision = %s",
            (project_id, feature_id, revision),
        )
        # Nothing is owed for it: a proposal waiting to be reconfirmed has no open gate
        # to be sent back at.
        return ""
    effective, _ = await _revisions_of(connection, project_id, feature_id)
    entries = await _entries(connection, project_id, feature_id, revision)
    why = (
        why_not_a_revision(
            await _members(connection, project_id, feature_id, effective),
            [Entry(entry["story_id"], entry["kind"], entry["corrects"]) for entry in entries],
        )
        or await _why_an_edit_is_out_of_date(connection, project_id, entries)
        # Another feature may have been given one of these names since publication.
        or await _why_a_name_is_taken(
            connection, project_id, [e["story_id"] for e in entries if e["kind"] == "new"]
        )
    )
    if why:
        return why
    if effective is not None:
        await connection.execute(
            "UPDATE roadmaps SET state = 'superseded' WHERE project_id = %s "
            "AND feature_id = %s AND revision = %s",
            (project_id, feature_id, effective),
        )
    await connection.execute(
        "UPDATE roadmaps SET state = 'approved' WHERE project_id = %s AND feature_id = %s "
        "AND revision = %s",
        (project_id, feature_id, revision),
    )
    basis = await feature_basis(connection, project_id, feature_id)
    for entry in entries:
        if entry["kind"] == "new":
            await connection.execute(
                "INSERT INTO stories (project_id, story_id, feature_id, roadmap_revision, "
                "position, title, outcome, criteria, current_on, corrects) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    project_id,
                    entry["story_id"],
                    feature_id,
                    revision,
                    entry["position"],
                    entry["title"],
                    entry["outcome"],
                    Jsonb(entry["criteria"]),
                    basis,
                    entry["corrects"],
                ),
            )
        elif entry["kind"] == "reworded":
            await connection.execute(
                "UPDATE stories SET title = %s, outcome = %s, criteria = %s, "
                "revision = revision + 1, current_on = story_basis(project_id, story_id) "
                "WHERE project_id = %s AND story_id = %s",
                (
                    entry["title"],
                    entry["outcome"],
                    Jsonb(entry["criteria"]),
                    project_id,
                    entry["story_id"],
                ),
            )
        elif entry["kind"] == "dropped":
            await connection.execute(
                "UPDATE stories SET state = 'withdrawn' WHERE project_id = %s AND story_id = %s",
                (project_id, entry["story_id"]),
            )
            await retire_all_of_story(connection, project_id, entry["story_id"])
    await retire_what_a_roadmap_answers(
        connection,
        project_id,
        feature_id,
        revisions=[revision, *([effective] if effective is not None else [])],
        stories=[e["story_id"] for e in entries if e["kind"] in ("reworded", "dropped")],
    )
    # The Analyst owed a correction for each rejection this revision now answers.
    await connection.execute(
        "UPDATE assignments SET state = 'done', grant_id = NULL, granted_to = NULL "
        "WHERE project_id = %s AND feature_id = %s AND kind = 'correct' "
        "AND state IN ('wanted', 'granted') AND issued_on->>'story_id' = ANY(%s)",
        (project_id, feature_id, [e["corrects"] for e in entries if e["corrects"] is not None]),
    )
    return ""


async def _settle_story(connection: Connection, story: Mapping[str, Any], outcome: str) -> None:
    if outcome == "approved":
        await connection.execute(
            "UPDATE stories SET state = 'approved' WHERE project_id = %s AND story_id = %s",
            (story["project_id"], story["story_id"]),
        )
        return
    if outcome == "rejected":
        await _reject(connection, story)
        return
    # A new revision of the same story: what was integrated and what was reviewed stay.
    await connection.execute(
        # The seal belonged to the revision that was reviewed; the new one has none.
        "UPDATE stories SET state = 'changes_requested', revision = revision + 1, "
        "sealed_revision = NULL "
        "WHERE project_id = %s AND story_id = %s",
        (story["project_id"], story["story_id"]),
    )


async def _reject(connection: Connection, story: Mapping[str, Any]) -> None:
    """Set a story aside for good. What it put on trunk stays there, and stays something
    every later candidate must still satisfy. For its own unfinished work this is what
    cancelling a feature is: nothing of it starts, what is on its way is kept as
    evidence and moves nothing, and a push already authorised runs to its end. The
    Analyst is owed a correction."""
    project_id, story_id = story["project_id"], story["story_id"]
    await connection.execute(
        "UPDATE stories SET state = 'rejected', seal_id = NULL, sealed_revision = NULL "
        "WHERE project_id = %s AND story_id = %s",
        (project_id, story_id),
    )
    await connection.execute(
        "UPDATE inbox_items SET state = 'closed' WHERE state = 'open' AND question_id IN "
        "(SELECT question_id FROM questions WHERE project_id = %s AND scope_story = %s "
        "AND state = 'open')",
        (project_id, story_id),
    )
    await connection.execute(
        "UPDATE questions SET state = 'withdrawn' WHERE project_id = %s AND scope_story = %s "
        "AND state = 'open'",
        (project_id, story_id),
    )
    await connection.execute(
        "UPDATE check_batches k SET state = 'abandoned', "
        "reason = 'the story was rejected before these checks could be run again' "
        "WHERE k.project_id = %s AND (k.state = 'held' OR (k.state = 'interrupted' AND EXISTS "
        "(SELECT 1 FROM assignments a WHERE a.kind = 'integrate' AND a.state = 'wanted' "
        "AND a.issued_on->>'batch_id' = k.batch_id::text))) AND k.behaviour_id IN "
        "(SELECT behaviour_id FROM behaviours WHERE project_id = %s AND story_id = %s)",
        (project_id, project_id, story_id),
    )
    # After the checks above were found by the assignments that wanted them.
    await retire_all_of_story(connection, project_id, story_id)
    await owe(
        connection,
        CORRECT,
        Envelope(
            message_kind=MessageKind.COMMAND,
            event_type="correction.requested",
            project_id=project_id,
            sender=CONTROL_PLANE,
            recipient="analyst",
            idempotency_key=f"correct:{project_id}:{story_id}",
            correlation_id=uuid.uuid7(),
            payload={"story_id": story_id, "feature_id": story["feature_id"]},
        ),
        issued_on={"story_id": story_id},
        feature_id=story["feature_id"],
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
        # The decisions in force over this story when the gate opened: the Product
        # Owner's answers and what a model assumed, told apart. A snapshot; the gate
        # decisions themselves are in the ledger, not here.
        "decisions": as_shown(
            await decisions_in_force(
                connection, story["project_id"], story["feature_id"], story["story_id"]
            )
        ),
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
            "SELECT assessment_id, statement, basis FROM story_assessments WHERE seal_id = %s",
            (story["seal_id"],),
        )
    ).fetchone()
