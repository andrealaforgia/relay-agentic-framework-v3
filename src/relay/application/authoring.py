"""Work whose reply is an artefact of Relay's own: a roadmap, a behaviour and its
contract, a seal, an assessment. Each is one call, and each reply is either what was
asked for or one question for the Product Owner, with whatever was assumed.

The model reads the project's trunk and changes nothing. What it replies is applied by
trusted code, in the transaction that marks the effect done: it is on record once, with
the mark, or not at all.
"""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from relay.application.assignments import ask_instead, complete
from relay.application.behaviours import rewrite_contract_in, specify_in
from relay.application.closure import decisions_in_force
from relay.application.decisions import Decisions, Evidence, _evidence_of, ask_in, assume_in
from relay.application.invocations import Effect, EffectRun, Invocations, Overtaken, Prepared
from relay.application.stories import (
    Dropped,
    Kept,
    Proposed,
    Reworded,
    StoryDraft,
    _revisions_of,
    assess_in,
    publish_roadmap_in,
    say_demonstrated,
    seal_in,
)
from relay.application.tasks import Profile, Workspaces
from relay.application.worker import Call
from relay.domain.behaviour import TransitionRefused
from relay.domain.evidence import Baseline, Expected, Launch, ProcessContract
from relay.domain.review import SubmissionRefused
from relay.domain.story import ActionRefused, Principal, PrincipalKind, SealRefused
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
        return [_refusing(effect) for effect in self._effects(invocation)]

    def _effects(self, invocation: Mapping[str, Any]) -> list[Effect]:
        """What was assumed is recorded in the transaction that accepts what it was
        assumed for, the artefact or the question: a reply that is refused leaves no
        assumption behind to be read as a decision. A question is the only effect of a
        reply that asks: it stops its scope, and nothing is applied in a stopped scope."""
        if invocation["reply"]["outcome"] == "question":
            return [Effect("ask", _nothing, self._ask)]
        first, *rest = self._deliver(invocation)
        accepted = first.settle

        async def with_what_was_assumed(
            connection: Connection, run: EffectRun
        ) -> Mapping[str, Any] | None:
            await self._assume(connection, run)
            return await accepted(connection, run) if accepted is not None else None

        return [Effect(first.name, first.perform, with_what_was_assumed), *rest]

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
        await self._assume(connection, run)
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


# What a service or the domain raises to say "not this": each is why the reply gave
# nothing, kept with the call, and never an error that leaves a paid reply unread.
_REFUSALS = (SealRefused, TransitionRefused, SubmissionRefused, ValueError, KeyError, TypeError)


def _refusing(effect: Effect) -> Effect:
    """The same effect, with every refusal of the reply said as one."""

    async def perform(run: EffectRun) -> Mapping[str, Any] | None:
        try:
            return await effect.perform(run)
        except _REFUSALS as refused:
            raise ActionRefused(f"{refused}") from None

    async def settle(connection: Connection, run: EffectRun) -> Mapping[str, Any] | None:
        assert effect.settle is not None
        try:
            return await effect.settle(connection, run)
        except _REFUSALS as refused:
            raise ActionRefused(f"{refused}") from None

    return Effect(effect.name, perform, settle if effect.settle is not None else None)


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
                # At its gate, or while it was being worked on: both are rejections.
                "s.project_id AND v.subject_id = s.story_id "
                "AND v.event_type IN ('gate.rejected', 'story.rejected') "
                "ORDER BY v.sequence DESC LIMIT 1) AS rejected_because "
                "FROM roadmap_entries e JOIN stories s ON s.project_id = e.project_id "
                "AND s.story_id = e.story_id WHERE e.project_id = %s AND e.feature_id = %s "
                "AND e.revision = %s AND e.kind <> 'dropped' ORDER BY e.position",
                (project_id, feature_id, effective),
            )
        ).fetchall()
        failed = await (
            await connection.execute(
                # What the feature's final check observed that did not hold, for a
                # roadmap that is to change because of it.
                "SELECT o.name, o.contract->'expect' AS expected, x.exit_code, "
                "convert_from(x.stdout, 'UTF8') AS stdout FROM completion_receipts r "
                "JOIN obligations o USING (obligation_id) JOIN executions x USING (execution_id) "
                "WHERE r.batch_id::text = %s AND NOT r.satisfied ORDER BY o.name",
                (assignment["issued_on"].get("batch_id"),),
            )
        ).fetchall()
        asked = await (
            await connection.execute(
                "SELECT payload->'said'->>'reason' AS reason FROM events WHERE project_id = %s "
                "AND subject_id = %s AND event_type = 'gate.changes_requested' "
                "AND payload->>'kind' = 'completion' ORDER BY sequence DESC LIMIT 1",
                (project_id, feature_id),
            )
        ).fetchone()
        return {
            "failed": failed,
            "asked": asked["reason"] if asked else None,
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
                    "roadmap to change"
                    + (f": {about['asked']}" if about["asked"] else ".")
                    + " Propose the change that would let the feature be completed."
                ),
                "What the final check observed that did not hold:",
                *(
                    f"  {each['name']}: expected {json.dumps(each['expected'], sort_keys=True)}, "
                    f"observed exit code {each['exit_code']} and output {each['stdout']!r}"
                    for each in about["failed"]
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
            submitted_for=work["assignment_id"],
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


_CONTRACT = {
    "type": "object",
    "properties": {
        "contract_id": {"type": "string"},
        "observes": {"type": "string"},
        "argv": {"type": "array", "items": {"type": "string"}},
        "timeout_seconds": {"type": "integer"},
        "expect_exit_code": {"type": "integer"},
        "expect_stdout": {"type": "string"},
        "baseline_exit_code": {"type": "integer"},
        "baseline_stdout": {"type": "string"},
        "baseline_stderr": {"type": "string"},
    },
    "required": [
        "contract_id",
        "observes",
        "argv",
        "timeout_seconds",
        "expect_exit_code",
        "expect_stdout",
        "baseline_exit_code",
    ],
    "additionalProperties": False,
}

_ABOUT_A_CONTRACT = [
    (
        "An acceptance contract is one command and what it must print. Relay launches it "
        "itself, in a container with no network, on a clean checkout: first on the trunk as "
        "it is, where it must fail in exactly the way you say (the baseline), and later on "
        "the Builder's candidate, where it must give exactly what you expect."
    ),
    "  contract_id: a name for it. observes: the public behaviour it observes, in words.",
    "  argv: the command, one argument per item. timeout_seconds: how long it may take.",
    "  expect_exit_code and expect_stdout: what the finished behaviour gives, exactly.",
    (
        "  baseline_exit_code, and one of baseline_stdout or baseline_stderr: what the trunk "
        "gives now, exactly. It must differ from what you expect: a test that already passes "
        "shows nothing."
    ),
    (
        "The command observes behaviour from outside, through what the program prints. It "
        "does not look at how the code is written."
    ),
]


def contract_from(reply: Mapping[str, Any]) -> ProcessContract:
    """The contract a reply amounts to. Raises ValueError if it is not one."""
    out, err = reply.get("baseline_stdout"), reply.get("baseline_stderr")
    return ProcessContract(
        contract_id=str(reply["contract_id"]),
        observes=str(reply["observes"]),
        launch=Launch(
            argv=tuple(str(each) for each in reply["argv"]),
            timeout_seconds=int(reply["timeout_seconds"]),
        ),
        expect=Expected(
            exit_code=int(reply["expect_exit_code"]), stdout=str(reply["expect_stdout"])
        ),
        baseline=Baseline(exit_code=int(reply["baseline_exit_code"]), stdout=out, stderr=err),
    )


async def _story(connection: Connection, project_id: str, story_id: str) -> dict[str, Any]:
    story = await (
        await connection.execute(
            "SELECT s.story_id, s.feature_id, s.title, s.outcome, s.criteria, s.revision, "
            "s.list_revision, s.seal_id, s.state FROM stories s "
            "WHERE s.project_id = %s AND s.story_id = %s",
            (project_id, story_id),
        )
    ).fetchone()
    assert story is not None
    story["decisions"] = await decisions_in_force(
        connection, project_id, story["feature_id"], story_id
    )
    story["behaviours"] = await (
        await connection.execute(
            "SELECT behaviour_id, summary, state, demonstrates, contract FROM behaviours "
            "WHERE project_id = %s AND story_id = %s ORDER BY position",
            (project_id, story_id),
        )
    ).fetchall()
    story["sent_back"] = await (
        await connection.execute(
            "SELECT payload->'said'->>'reason' AS reason FROM events WHERE project_id = %s "
            "AND subject_id = %s AND event_type = 'gate.changes_requested' "
            "ORDER BY sequence DESC LIMIT 1",
            (project_id, story_id),
        )
    ).fetchone()
    return story


def _story_lines(story: Mapping[str, Any]) -> list[str]:
    lines = [
        f"Story {story['story_id']}, {story['title']}: {story['outcome']}",
        "Its acceptance criteria:",
        *(f"  {name}: {text}" for name, text in sorted(story["criteria"].items())),
    ]
    if story["decisions"]:
        lines += [
            "",
            (
                "Decisions in force (the Product Owner's bind you; a model's are "
                "assumptions a person may overrule):"
            ),
        ]
        lines += [
            f"  {each['number']} ({each['source'].replace('_', ' ')}): {each['text']}"
            for each in story["decisions"]
        ]
    if story["behaviours"]:
        lines += ["", "Its behaviours so far, in order:"]
        lines += [
            f"  {each['behaviour_id']} [{each['state'].replace('_', ' ')}] {each['summary']} "
            f"(demonstrates {', '.join(each['demonstrates'])})"
            for each in story["behaviours"]
        ]
    else:
        lines += ["", "It has no behaviours yet."]
    if story["state"] == "changes_requested" and story["sent_back"]:
        lines += ["", f"The Product Owner sent the story back: {story['sent_back']['reason']}"]
    return lines


class SpecifyTask(Authoring):
    """The Verifier says what a story needs next: one more behaviour, with the contract
    that will show it, or nothing more. For a behaviour whose red showed its contract
    cannot serve, it writes the contract again."""

    role = "verifier"
    delivered: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "demonstrated": {"type": "boolean"},
            "behaviour_id": {"type": "string"},
            "summary": {"type": "string"},
            "demonstrates": {"type": "array", "items": {"type": "string"}},
            "contract": _CONTRACT,
        },
        "required": ["demonstrated"],
        "additionalProperties": False,
    }

    async def _about(self, connection: Connection, assignment: Mapping[str, Any]) -> dict[str, Any]:
        about = await _story(connection, assignment["project_id"], assignment["story_id"])
        about["behaviour_id"] = assignment["behaviour_id"]
        about["invalid_red"] = None
        if assignment["behaviour_id"] is not None:
            command = await (
                await connection.execute(
                    "SELECT envelope->'payload'->>'invalid_red' AS why FROM outbox "
                    "WHERE message_id = %s",
                    (assignment["command_message_id"],),
                )
            ).fetchone()
            about["invalid_red"] = command["why"] if command else None
        return about

    def _briefing(self, about: Mapping[str, Any]) -> list[str]:
        lines = [
            (
                "You are the Verifier. You decide how a user story is shown to be delivered: "
                "one behaviour at a time, each with an acceptance contract that Relay itself "
                "observes. You write no production code."
            ),
            "",
            *_story_lines(about),
            "",
            *_ABOUT_A_CONTRACT,
            "",
        ]
        if about["behaviour_id"] is not None:
            [behaviour] = [
                each
                for each in about["behaviours"]
                if each["behaviour_id"] == about["behaviour_id"]
            ]
            lines += [
                (
                    f"The contract of behaviour {about['behaviour_id']} was run on the trunk and "
                    f"cannot serve: {about['invalid_red']}"
                ),
                "The contract as it is:",
                json.dumps(behaviour["contract"], indent=2, sort_keys=True),
                "",
                (
                    "What to deliver: demonstrated false, and the contract written again for "
                    "that behaviour. Give no behaviour_id, summary or demonstrates."
                ),
            ]
        else:
            lines += [
                (
                    "What to deliver: the next behaviour this story needs, the smallest that "
                    "moves it forward (demonstrated false, with behaviour_id, summary, the "
                    "criteria it demonstrates, and its contract); or, if every criterion is "
                    "already shown by the behaviours on trunk, demonstrated true and nothing "
                    "else. A behaviour's identifier is one no behaviour of the project has had."
                ),
            ]
        return lines

    def _evidence(self, about: Mapping[str, Any]) -> dict[str, Any]:
        return {"rewrites": about["behaviour_id"]}

    def _problem_with_delivered(self, delivered: Any) -> str | None:
        if delivered.get("demonstrated"):
            if set(delivered) != {"demonstrated"}:
                return "a story that is demonstrated needs nothing else said"
            return None
        try:
            contract_from(delivered["contract"])
        except (KeyError, TypeError, ValueError) as malformed:
            return f"the contract is not in the form asked for: {malformed}"
        return None

    def _deliver(self, invocation: Mapping[str, Any]) -> list[Effect]:
        return [Effect("specify", _nothing, self._specify)]

    async def _specify(self, connection: Connection, run: EffectRun) -> Mapping[str, Any] | None:
        work = await _work(connection, run)
        delivered = run.invocation["reply"]["delivered"]
        expects, caller = run.invocation["expects"], caller_of(run.invocation)
        project_id = work["project_id"]
        await complete(connection, work["assignment_id"])
        if work["behaviour_id"] is not None:
            if delivered.get("demonstrated"):
                raise ActionRefused("what was asked for is the contract, written again")
            await rewrite_contract_in(
                connection,
                caller,
                project_id,
                work["behaviour_id"],
                contract_from(delivered["contract"]),
                basis=expects["basis"],
            )
            return None
        if delivered.get("demonstrated"):
            await say_demonstrated(
                connection,
                caller,
                project_id,
                work["story_id"],
                story_revision=expects["story_revision"],
                list_revision=expects["list_revision"],
                basis=expects["basis"],
            )
            return None
        if not delivered.get("behaviour_id") or not delivered.get("demonstrates"):
            raise ActionRefused("a behaviour needs its identifier and what it demonstrates")
        try:
            await specify_in(
                connection,
                caller,
                project_id,
                work["story_id"],
                str(delivered["behaviour_id"]),
                str(delivered.get("summary", "")),
                [str(each) for each in delivered["demonstrates"]],
                contract_from(delivered["contract"]),
                basis=expects["basis"],
                story_revision=expects["story_revision"],
            )
        except ValueError as refused:
            raise ActionRefused(str(refused)) from None
        # The list has one more behaviour, by this effect's own doing.
        return {"list_revision": expects["list_revision"] + 1}


_NOTE = {
    "type": "object",
    "properties": {"statement": {"type": "string"}},
    "required": ["statement"],
    "additionalProperties": False,
}


class SealTask(Authoring):
    """The Verifier vouches that the behaviours on trunk, and no others, show the
    story's criteria."""

    role = "verifier"
    delivered: Mapping[str, Any] = _NOTE

    async def _about(self, connection: Connection, assignment: Mapping[str, Any]) -> dict[str, Any]:
        return await _story(connection, assignment["project_id"], assignment["story_id"])

    def _briefing(self, about: Mapping[str, Any]) -> list[str]:
        return [
            (
                "You are the Verifier. Every behaviour of this story is on the trunk. You are "
                "asked to vouch that these behaviours, and no others, show the story's "
                "acceptance criteria."
            ),
            "",
            *_story_lines(about),
            "",
            (
                "What to deliver: a statement, in a sentence or two, of how the behaviours show "
                "the criteria. Delivering it is vouching for it. If they do not show them, do "
                "not vouch: ask the Product Owner what should happen."
            ),
        ]

    def _problem_with_delivered(self, delivered: Any) -> str | None:
        return None if str(delivered.get("statement", "")).strip() else "a statement says something"

    def _deliver(self, invocation: Mapping[str, Any]) -> list[Effect]:
        return [Effect("seal", _nothing, self._seal)]

    async def _seal(self, connection: Connection, run: EffectRun) -> None:
        work, expects = await _work(connection, run), run.invocation["expects"]
        try:
            await seal_in(
                connection,
                caller_of(run.invocation),
                work["project_id"],
                work["story_id"],
                story_revision=expects["story_revision"],
                list_revision=expects["list_revision"],
                basis=expects["basis"],
            )
        except ValueError as refused:
            raise ActionRefused(str(refused)) from None


class AssessTask(Authoring):
    """The Analyst says, for the Product Owner to read, whether the sealed behaviours
    demonstrate the story's outcome."""

    role = "analyst"
    delivered: Mapping[str, Any] = _NOTE

    async def _about(self, connection: Connection, assignment: Mapping[str, Any]) -> dict[str, Any]:
        return await _story(connection, assignment["project_id"], assignment["story_id"])

    def _evidence(self, about: Mapping[str, Any]) -> dict[str, Any]:
        return {"seal": str(about["seal_id"]) if about["seal_id"] else None}

    def _briefing(self, about: Mapping[str, Any]) -> list[str]:
        return [
            (
                "You are the Analyst. The Verifier has sealed this story's behaviours: all of "
                "them are on the trunk. You are asked to assess whether, together, they "
                "demonstrate the outcome the story is for. The Product Owner reads what you "
                "write, as you write it, before deciding whether to accept the story."
            ),
            "",
            *_story_lines(about),
            "",
            (
                "What to deliver: your assessment as a statement, plain and specific: what is "
                "demonstrated, and anything that is not."
            ),
        ]

    def _problem_with_delivered(self, delivered: Any) -> str | None:
        return None if str(delivered.get("statement", "")).strip() else "a statement says something"

    def _deliver(self, invocation: Mapping[str, Any]) -> list[Effect]:
        return [Effect("assess", _nothing, self._assess)]

    async def _assess(self, connection: Connection, run: EffectRun) -> None:
        work, expects = await _work(connection, run), run.invocation["expects"]
        seal = run.invocation["evidence"]["seal"]
        if seal is None:
            raise Overtaken("the story had no seal when this assessment was asked for")
        await assess_in(
            connection,
            caller_of(run.invocation),
            work["project_id"],
            work["story_id"],
            str(run.invocation["reply"]["delivered"]["statement"]),
            story_revision=expects["story_revision"],
            list_revision=expects["list_revision"],
            basis=expects["basis"],
            seal=uuid.UUID(seal),
        )


def _revision_lines(stories: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = []
    for each in stories:
        corrects = f", correcting {each['corrects']}" if each["corrects"] else ""
        lines.append(
            f"  {each['story_id']} [{each['kind']}{corrects}] {each['title']}: {each['outcome']}"
        )
        if each["kind"] != "dropped":
            lines += [
                f"      {name}: {text}" for name, text in sorted((each["criteria"] or {}).items())
            ]
    return lines


class ReconfirmTask(Authoring):
    """After a decision of the Product Owner's, each owner says whether what is theirs
    still stands: the Analyst for a roadmap or a story, the Verifier for a contract,
    which it may amend instead. The services that record this answer the same statement
    under the same grant once, however often it is made."""

    delivered: Mapping[str, Any] = {
        "type": "object",
        "properties": {"verdict": {"enum": ["stands", "amended"]}, "contract": _CONTRACT},
        "required": ["verdict"],
        "additionalProperties": False,
    }

    def __init__(self, role: str, decisions: Decisions, *arguments: Any, **named: Any) -> None:
        super().__init__(*arguments, **named)
        self.role = role
        self._decisions = decisions

    async def _about(self, connection: Connection, assignment: Mapping[str, Any]) -> dict[str, Any]:
        project_id = assignment["project_id"]
        about: dict[str, Any] = {
            "subject": assignment["subject"],
            "feature_id": assignment["feature_id"],
            "revision": assignment["issued_on"].get("revision"),
            "decisions": await decisions_in_force(
                connection, project_id, assignment["feature_id"], assignment["story_id"]
            ),
        }
        if assignment["subject"] == "roadmap":
            # From the revision as it is stored, whether or not it is in force: a story
            # a proposal adds is no story yet, and is exactly what is being vouched for.
            about["stories"] = await (
                await connection.execute(
                    "SELECT e.story_id, e.kind, coalesce(e.title, s.title) AS title, "
                    "coalesce(e.outcome, s.outcome) AS outcome, "
                    "coalesce(e.criteria, s.criteria) AS criteria, e.corrects "
                    "FROM roadmap_entries e LEFT JOIN stories s ON s.project_id = e.project_id "
                    "AND s.story_id = e.story_id WHERE e.project_id = %s AND e.feature_id = %s "
                    "AND e.revision = %s ORDER BY e.kind = 'dropped', e.position",
                    (project_id, assignment["feature_id"], about["revision"]),
                )
            ).fetchall()
            return about
        about["story"] = await _story(connection, project_id, assignment["story_id"])
        if assignment["subject"] == "behaviour":
            [behaviour] = [
                each
                for each in about["story"]["behaviours"]
                if each["behaviour_id"] == assignment["behaviour_id"]
            ]
            about["behaviour"] = behaviour
            about["evidence"] = await _evidence_of(
                connection, project_id, assignment["behaviour_id"]
            )
        return about

    def _evidence(self, about: Mapping[str, Any]) -> dict[str, Any]:
        if about["subject"] != "behaviour":
            return {}
        contract = ProcessContract.from_dict(about["behaviour"]["contract"])
        return {"contract_digest": contract.digest, "retains": asdict(about["evidence"])}

    def _briefing(self, about: Mapping[str, Any]) -> list[str]:
        decided = [
            "The decisions now in force:",
            *(
                f"  {each['number']} ({each['source'].replace('_', ' ')}): {each['text']}"
                for each in about["decisions"]
            ),
            "",
        ]
        if about["subject"] == "roadmap":
            return [
                (
                    "You are the Analyst. The Product Owner has made a decision since this "
                    f"roadmap (revision {about['revision']} of feature {about['feature_id']}) "
                    "was written. You are asked whether it still stands under the decisions "
                    "now in force."
                ),
                "",
                *decided,
                (
                    "The roadmap, as this revision has it (each story with what the revision "
                    "does to it: kept, new, reworded or dropped):"
                ),
                *_revision_lines(about["stories"]),
                "",
                (
                    'What to deliver: verdict "stands", if it does. If it does not, ask the '
                    "Product Owner what should change."
                ),
            ]
        if about["subject"] == "story":
            return [
                (
                    "You are the Analyst. The Product Owner has made a decision since this "
                    "story was written. You are asked whether its text and criteria still "
                    "stand under the decisions now in force."
                ),
                "",
                *_story_lines(about["story"]),
                "",
                (
                    'What to deliver: verdict "stands", if it does. If it does not, ask the '
                    "Product Owner what should change."
                ),
            ]
        return [
            (
                "You are the Verifier. The Product Owner has made a decision since the "
                f"contract of behaviour {about['behaviour']['behaviour_id']} was written. You "
                "are asked whether the contract, and everything observed under it, still "
                "stands under the decisions now in force."
            ),
            "",
            *_story_lines(about["story"]),
            "",
            "The contract:",
            json.dumps(about["behaviour"]["contract"], indent=2, sort_keys=True),
            "",
            *_ABOUT_A_CONTRACT,
            "",
            (
                'What to deliver: verdict "stands", if all of it does; or verdict "amended" '
                "with the contract as it should now be, which sets aside everything gathered "
                "for the old one."
            ),
        ]

    def _problem_with_delivered(self, delivered: Any) -> str | None:
        if delivered.get("verdict") != "amended":
            return None if "contract" not in delivered else "a contract that stands is not given"
        try:
            contract_from(delivered["contract"])
        except (KeyError, TypeError, ValueError) as malformed:
            return f"the contract is not in the form asked for: {malformed}"
        return None

    def _deliver(self, invocation: Mapping[str, Any]) -> list[Effect]:
        return [Effect("reconfirm", self._reconfirm)]

    async def _reconfirm(self, run: EffectRun) -> None:
        async with self._pool.connection() as connection:
            work = await _work(connection, run)
        delivered, read = run.invocation["reply"]["delivered"], run.invocation["evidence"]
        caller, basis = caller_of(run.invocation), run.invocation["expects"]["basis"]
        project_id = work["project_id"]
        if work["subject"] != "behaviour" and delivered["verdict"] != "stands":
            raise ActionRefused("a roadmap or a story stands, or the Product Owner is asked")
        if work["subject"] == "roadmap":
            await self._decisions.reconfirm_roadmap(
                caller,
                project_id,
                work["feature_id"],
                grant=run.grant_id,
                basis=basis,
                revision=work["issued_on"]["revision"],
            )
        elif work["subject"] == "story":
            await self._decisions.reconfirm_story(
                caller,
                project_id,
                work["story_id"],
                grant=run.grant_id,
                basis=basis,
                revision=run.invocation["expects"]["story_revision"],
            )
        elif delivered["verdict"] == "stands":
            await self._decisions.reconfirm_contract(
                caller,
                project_id,
                work["behaviour_id"],
                grant=run.grant_id,
                basis=basis,
                contract_digest=read["contract_digest"],
                evidence=Evidence(**read["retains"]),
            )
        else:
            await self._decisions.amend_contract(
                caller,
                project_id,
                work["behaviour_id"],
                contract_from(delivered["contract"]),
                grant=run.grant_id,
                basis=basis,
                replaces=read["contract_digest"],
            )
