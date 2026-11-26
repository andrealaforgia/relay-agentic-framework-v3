"""Work whose reply is an artefact of Relay's own: a roadmap, a behaviour and its
contract, a seal, an assessment. Each is one call, and each reply is either what was
asked for or one question for the Product Owner, with whatever was assumed.

The model reads the project's trunk and changes nothing. What it replies is applied by
trusted code, in the transaction that marks the effect done: it is on record once, with
the mark, or not at all.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from relay.application.assignments import ask_instead
from relay.application.closure import decisions_in_force
from relay.application.decisions import ask_in, assume_in
from relay.application.invocations import Effect, EffectRun, Invocations, Overtaken, Prepared
from relay.application.stories import (
    Dropped,
    Kept,
    Proposed,
    Reworded,
    StoryDraft,
    _revisions_of,
    publish_roadmap_in,
)
from relay.application.tasks import Profile, Workspaces
from relay.application.worker import Call
from relay.domain.story import ActionRefused, Principal, PrincipalKind
from relay.git.checkout import resolve_ref
from relay.git.mirror import Mirror
from relay.persistence.database import Connection, Pool
from relay.providers.confinement import confinement_for, effective_agent_sha256
from relay.providers.definitions import DEFAULT_DEFINITIONS, find_definition

_QUESTION = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "why": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["text", "why", "options"],
    "additionalProperties": False,
}


def reply_schema(delivered: Mapping[str, Any]) -> dict[str, Any]:
    """The shape every reply of this kind of work has: what was asked for, or a question
    in its place, and what was assumed either way."""
    return {
        "type": "object",
        "properties": {
            "outcome": {"enum": ["delivered", "question"]},
            "assumptions": {"type": "array", "items": {"type": "string"}},
            "delivered": dict(delivered),
            "question": _QUESTION,
        },
        "required": ["outcome", "assumptions"],
        "additionalProperties": False,
    }


_HOW_TO_REPLY = [
    "",
    (
        'Reply with outcome "delivered" and what is asked for above. If you cannot do that '
        "without a decision that is the Product Owner's to make (what the product should do, "
        'not how), reply with outcome "question" instead: one question, why it matters, and '
        "the options you see. Do not ask what you can settle by reading."
    ),
    (
        "List under assumptions anything you took to be so without being told: each is shown "
        "to the Product Owner, who may overrule it. An empty list if there are none."
    ),
]


async def _nothing(run: EffectRun) -> None:
    """What these effects do, they do where they are marked done."""


def caller_of(invocation: Mapping[str, Any]) -> Principal:
    """The worker whose call this was: what it replied is done in its name."""
    role = str(invocation["role"])
    return Principal(PrincipalKind.WORKER, f"{role}@{invocation['worker_id']}", role)


class Authoring:
    """What every such task does the same way."""

    role: str

    def __init__(
        self,
        pool: Pool,
        invocations: Invocations,
        workspaces: Workspaces,
        mirrors: Path,
        definitions: Sequence[Path],
        profile: Profile,
        remote_timeout: float = 120.0,
    ) -> None:
        self._pool = pool
        self._invocations = invocations
        self._workspaces = workspaces
        self._mirrors = mirrors
        self._definitions = definitions
        self._profile = profile
        self._remote_timeout = remote_timeout

    # What a kind of work says for itself.

    delivered: Mapping[str, Any]

    async def _about(self, connection: Connection, assignment: Mapping[str, Any]) -> dict[str, Any]:
        """What the briefing is written from, read in one snapshot."""
        raise NotImplementedError

    def _briefing(self, about: Mapping[str, Any]) -> list[str]:
        raise NotImplementedError

    def _evidence(self, about: Mapping[str, Any]) -> dict[str, Any]:
        """What of that the reply will be applied against, recorded with the call."""
        return {}

    def _problem_with_delivered(self, delivered: Any) -> str | None:
        raise NotImplementedError

    def _deliver(self, invocation: Mapping[str, Any]) -> list[Effect]:
        raise NotImplementedError

    # What they all do.

    async def prepare(
        self, assignment: Mapping[str, Any], grant: uuid.UUID, last_problem: str | None
    ) -> Call:
        read_on = await self._invocations.as_it_stands(assignment["assignment_id"])
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            about = await self._about(connection, assignment)
            source = await (
                await connection.execute(
                    "SELECT p.repository, p.trunk, o.envelope->'payload'->>'reason' AS reason "
                    "FROM assignments a JOIN projects p USING (project_id) "
                    "JOIN outbox o ON o.message_id = a.command_message_id "
                    "WHERE a.assignment_id = %s",
                    (assignment["assignment_id"],),
                )
            ).fetchone()
        assert source is not None
        workspace = self._workspaces.fresh()
        workspace.mkdir(parents=True)
        trunk = await self._export_trunk(str(assignment["project_id"]), source, workspace)
        definition = find_definition(DEFAULT_DEFINITIONS[self.role], self._definitions)
        bounds = confinement_for(self.role, workspace)
        lines = [
            *self._briefing(about),
            "",
            (
                f"The project's code as it is on its trunk (commit {trunk}) is in your working "
                "directory. You can read it and search it. You cannot change it."
                if trunk
                else "The project's repository has nothing on its trunk yet."
            ),
        ]
        if source["reason"]:
            lines += [
                "",
                f"This work was owed before, and is owed again because {source['reason']}",
            ]
        lines += _HOW_TO_REPLY
        if last_problem:
            lines += [
                "",
                f"Your last reply could not be used: {last_problem}. Reply again in full.",
            ]
        return Call(
            Prepared(
                provider="claude",
                model=self._profile.model,
                effort=self._profile.effort,
                definition=definition,
                effective_agent_sha256=effective_agent_sha256(definition, bounds.tools),
                briefing="\n".join(lines) + "\n",
                schema=reply_schema(self.delivered),
                read_on=read_on,
                evidence={**self._evidence(about), "trunk": trunk},
            ),
            workspace,
            bounds,
            self._profile.timeout,
        )

    async def _export_trunk(
        self, project_id: str, source: Mapping[str, Any], workspace: Path
    ) -> str | None:
        head = await resolve_ref(
            source["repository"], f"refs/heads/{source['trunk']}", timeout=self._remote_timeout
        )
        if head is None:
            return None
        mirror = Mirror(self._mirrors / f"{project_id}.git")
        if not await mirror.recover(
            source["repository"],
            head,
            [f"refs/heads/{source['trunk']}"],
            timeout=self._remote_timeout,
        ):
            raise ActionRefused("the trunk moved while it was being read: it is read again")
        workspace.rmdir()
        await mirror.export(head, workspace)
        return head

    def problem_with(self, reply: Any) -> str | None:
        if not isinstance(reply, dict):
            return "the reply is an object"
        asked, delivered = reply.get("question"), reply.get("delivered")
        if reply.get("outcome") == "question":
            if not isinstance(asked, dict) or delivered is not None:
                return "a reply that asks has a question and delivers nothing"
            if not str(asked.get("text", "")).strip() or not str(asked.get("why", "")).strip():
                return "a question needs its text and why it matters"
            if not all(str(option).strip() for option in asked.get("options", ())):
                return "an option of a question says nothing"
            return None
        if delivered is None or asked is not None:
            return "a reply that delivers has what was asked for and no question"
        if not all(str(text).strip() for text in reply.get("assumptions", ())):
            return "an assumption says nothing"
        return self._problem_with_delivered(delivered)

    async def retain(
        self, assignment: Mapping[str, Any], call: Call, invocation_id: uuid.UUID
    ) -> str | None:
        return None  # the reply is all there is of it

    def discard(self, call: Call) -> None:
        shutil.rmtree(call.workdir.parent, ignore_errors=True)

    def effects(self, invocation: Mapping[str, Any]) -> list[Effect]:
        """Assumptions first: what they are recorded beside is made after them. A
        question last, and alone: it stops its scope, and nothing is applied in a scope
        that is stopped."""
        reply = invocation["reply"]
        first = [Effect("assume", _nothing, self._assume)] if reply["assumptions"] else []
        if reply["outcome"] == "question":
            return [*first, Effect("ask", _nothing, self._ask)]
        return [*first, *self._deliver(invocation)]

    async def _assume(self, connection: Connection, run: EffectRun) -> None:
        work = await _work(connection, run)
        for text in run.invocation["reply"]["assumptions"]:
            await assume_in(
                connection,
                caller_of(run.invocation),
                work["project_id"],
                work["feature_id"],
                story_id=work["story_id"],
                text=str(text),
            )

    async def _ask(self, connection: Connection, run: EffectRun) -> None:
        """The question, and the end of this occasion, together: answering it owes the
        work again."""
        work = await _work(connection, run)
        asked = run.invocation["reply"]["question"]
        await ask_in(
            connection,
            caller_of(run.invocation),
            f"invocation:{run.invocation['invocation_id']}",
            work["project_id"],
            work["feature_id"],
            story_id=work["story_id"],
            text=str(asked["text"]),
            why=str(asked["why"]),
            options=[str(option) for option in asked["options"]],
            asked_for=work["assignment_id"],
        )
        await ask_instead(connection, work["assignment_id"])


async def _work(connection: Connection, run: EffectRun) -> dict[str, Any]:
    row = await (
        await connection.execute(
            "SELECT * FROM assignments WHERE assignment_id = %s",
            (run.invocation["assignment_id"],),
        )
    ).fetchone()
    assert row is not None
    return row


_STORY = {
    "type": "object",
    "properties": {
        "change": {"enum": ["new", "kept", "reworded", "dropped"]},
        "story_id": {"type": "string"},
        "title": {"type": "string"},
        "outcome": {"type": "string"},
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        },
        "corrects": {"type": "string"},
    },
    "required": ["change", "story_id"],
    "additionalProperties": False,
}


class RoadmapTask(Authoring):
    """The Analyst proposes a feature's roadmap: its first (`plan`), a change to it
    after a final check failed (`revise`), or one that answers a rejected story
    (`correct`). The proposal changes nothing until a person approves it."""

    role = "analyst"
    delivered: Mapping[str, Any] = {
        "type": "object",
        "properties": {"stories": {"type": "array", "items": _STORY}},
        "required": ["stories"],
        "additionalProperties": False,
    }

    async def _about(self, connection: Connection, assignment: Mapping[str, Any]) -> dict[str, Any]:
        project_id, feature_id = assignment["project_id"], assignment["feature_id"]
        feature = await (
            await connection.execute(
                "SELECT problem FROM features WHERE project_id = %s AND feature_id = %s",
                (project_id, feature_id),
            )
        ).fetchone()
        assert feature is not None
        effective, proposed = await _revisions_of(connection, project_id, feature_id)
        stories = await (
            await connection.execute(
                "SELECT s.story_id, s.title, s.outcome, s.criteria, s.state, s.revision, "
                "s.corrects, story_basis(s.project_id, s.story_id) AS basis, "
                "(SELECT v.payload->'said'->>'reason' FROM events v WHERE v.project_id = "
                "s.project_id AND v.subject_id = s.story_id AND v.event_type = 'gate.rejected' "
                "ORDER BY v.sequence DESC LIMIT 1) AS rejected_because "
                "FROM roadmap_entries e JOIN stories s ON s.project_id = e.project_id "
                "AND s.story_id = e.story_id WHERE e.project_id = %s AND e.feature_id = %s "
                "AND e.revision = %s AND e.kind <> 'dropped' ORDER BY e.position",
                (project_id, feature_id, effective),
            )
        ).fetchall()
        return {
            "kind": assignment["kind"],
            "feature_id": feature_id,
            "problem": feature["problem"],
            "decisions": await decisions_in_force(connection, project_id, feature_id),
            "effective": effective,
            "proposed": proposed,
            "stories": stories,
            "issued_on": assignment["issued_on"],
        }

    def _evidence(self, about: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "revises": about["effective"],
            "replaces": about["proposed"],
            # Each story as it was read: an edit of one is an edit of that revision,
            # under those decisions, and of no later one.
            "stories": {
                each["story_id"]: {"revision": each["revision"], "basis": each["basis"]}
                for each in about["stories"]
            },
        }

    def _briefing(self, about: Mapping[str, Any]) -> list[str]:
        lines = [
            (
                "You are the Analyst. You propose the roadmap of one feature: its user stories, "
                "in the order they are to be delivered. A person approves your proposal or sends "
                "it back; nothing you propose is built until then."
            ),
            "",
            f"Feature {about['feature_id']}. The problem it is for:",
            about["problem"],
        ]
        if about["decisions"]:
            lines += [
                "",
                (
                    "Decisions in force (the Product Owner's bind you; a model's are "
                    "assumptions a person may overrule):"
                ),
            ]
            lines += [
                f"  {each['number']} ({each['source'].replace('_', ' ')}): {each['text']}"
                for each in about["decisions"]
            ]
        if about["stories"]:
            lines += ["", f"The roadmap in force (revision {about['effective']}):"]
            for each in about["stories"]:
                lines.append(
                    f"  {each['story_id']} [{each['state'].replace('_', ' ')}] "
                    f"{each['title']}: {each['outcome']}"
                )
                lines += [
                    f"      {name}: {text}" for name, text in sorted(each["criteria"].items())
                ]
                if each["state"] == "rejected":
                    lines.append(
                        "      Rejected by the Product Owner"
                        + (f": {each['rejected_because']}" if each["rejected_because"] else ".")
                    )
        else:
            lines += ["", "The feature has no roadmap yet."]
        if about["kind"] == "correct":
            lines += [
                "",
                (
                    f"Story {about['issued_on'].get('story_id')} was rejected. Your proposal "
                    "has to answer that rejection: with one new story that corrects it, or by "
                    "leaving it answered by what is already there."
                ),
            ]
        if about["kind"] == "revise":
            lines += [
                "",
                (
                    "The feature's final check failed and the Product Owner asked for the "
                    "roadmap to change. Propose the change that would let the feature be completed."
                ),
            ]
        lines += [
            "",
            (
                "What to deliver: the stories of the roadmap, in order. Each story is small, "
                "valuable on its own, independent of the ones after it, and testable: its "
                "acceptance criteria are what would be observed if it were delivered."
            ),
            "For each story say what it is against the roadmap in force:",
            (
                '  "new": give story_id, title, outcome and criteria (each with an id and a '
                "text). If it corrects a rejected story, name that story under corrects."
            ),
            (
                '  "kept": give only story_id. A story that is finished, under way or rejected '
                "can only be kept, and those keep their order."
            ),
            '  "reworded": a planned story with a new title, outcome and criteria.',
            '  "dropped": a planned story left out.',
            (
                "Every story of the roadmap in force is accounted for, exactly once. An "
                "identifier is letters, digits, dots, dashes and underscores, and a new story's "
                "is one no story of the project has had."
            ),
        ]
        return lines

    def _problem_with_delivered(self, delivered: Any) -> str | None:
        try:
            if not _entries(delivered, {}):
                return "a roadmap needs at least one story"
        except (KeyError, TypeError, ValueError) as malformed:
            return f"the roadmap is not in the form asked for: {malformed}"
        return None

    def _deliver(self, invocation: Mapping[str, Any]) -> list[Effect]:
        return [Effect("propose", _nothing, self._propose)]

    async def _propose(self, connection: Connection, run: EffectRun) -> None:
        work = await _work(connection, run)
        read = run.invocation["evidence"]
        now = await _revisions_of(connection, work["project_id"], work["feature_id"])
        if now != (read["revises"], read["replaces"]):
            # Another proposal was made or decided while this one was being written.
            # Nothing is wrong with the reply; it is about a roadmap that is not there.
            raise Overtaken("the roadmap moved on while this proposal was being written")
        await publish_roadmap_in(
            connection,
            caller_of(run.invocation),
            work["project_id"],
            work["feature_id"],
            _entries(run.invocation["reply"]["delivered"], read["stories"]),
            basis=run.invocation["expects"]["basis"],
            revises=read["revises"],
            replaces=read["replaces"],
        )


def _entries(delivered: Any, read: Mapping[str, Any]) -> list[Proposed]:
    """The proposal a reply amounts to. An edit of a story is of the story as it was
    read, which is on record with the call and not for the model to say."""
    entries: list[Proposed] = []
    for each in delivered["stories"]:
        story_id, change = str(each["story_id"]), each["change"]
        as_read = read.get(story_id, {"revision": 0, "basis": 0})
        if change == "kept":
            entries.append(Kept(story_id))
        elif change == "dropped":
            entries.append(Dropped(story_id, as_read["revision"], as_read["basis"]))
        else:
            criteria = {str(c["id"]): str(c["text"]) for c in each["criteria"]}
            if len(criteria) != len(each["criteria"]):
                raise ValueError(f"{story_id} names a criterion twice")
            title, outcome = str(each["title"]), str(each["outcome"])
            if change == "reworded":
                entries.append(
                    Reworded(
                        story_id, title, outcome, criteria, as_read["revision"], as_read["basis"]
                    )
                )
            else:
                entries.append(
                    StoryDraft(story_id, title, outcome, criteria, each.get("corrects") or None)
                )
    return entries
