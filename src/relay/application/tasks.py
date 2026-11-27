"""The kinds of work a worker carries out, each as: what the model is briefed with, what
it may reply, and what trusted code then does with the reply.

A role's restriction is not left to the prose of its agent definition. It is the shape
of the reply: a reviewer's reply has no field through which a file could be changed, and
its run has no tool that could change one.
"""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relay.application.assignments import ask_instead
from relay.application.behaviours import Behaviours
from relay.application.decisions import ask_in, assume_in
from relay.application.invocations import Effect, EffectRun, Invocations, Overtaken, Prepared
from relay.application.reviews import NewFinding, Review, Reviews
from relay.application.worker import Call
from relay.domain.review import Carried, Severity, SubmissionRefused
from relay.domain.schema import problem_with as schema_problem
from relay.domain.story import ActionRefused, Principal, PrincipalKind
from relay.git.checkout import CandidateMoved, RemoteRejected, attempt_ref, resolve_ref
from relay.git.mirror import Mirror
from relay.persistence.database import Connection, Pool
from relay.providers.confinement import confinement_for, effective_agent_sha256
from relay.providers.definitions import DEFAULT_DEFINITIONS, find_definition

QUESTION = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "why": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["text", "why", "options"],
    "additionalProperties": False,
}
_ASSUMPTIONS = {"type": "array", "items": {"type": "string"}}

ASKING_INSTEAD = (
    "If you cannot do this without a decision that is the Product Owner's to make (what "
    "the product should do, not how), reply with a question instead and nothing else: its "
    "text, why it matters, and the options you see. Do not ask what you can settle by "
    "reading. Under assumptions, list anything you took to be so without being told."
)


def asks(reply: Any) -> bool:
    """Whether a reply is a question for the Product Owner in place of the work."""
    return isinstance(reply, dict) and reply.get("question") is not None


def plain(reply: Any) -> Any:
    """A build's or a review's reply in its own flat form, whichever of the two forms it
    came in: its own (`note`, or `findings` and `carried`), or the form every other
    kind of work replies in (`outcome`, and what was asked for under `delivered`)."""
    if not isinstance(reply, dict) or not ({"outcome", "delivered"} & set(reply)):
        return reply
    delivered = reply.get("delivered")
    rest = {name: value for name, value in reply.items() if name not in ("outcome", "delivered")}
    return {**rest, **delivered} if isinstance(delivered, dict) else rest


def _shape_problem(reply: Any, flat: Mapping[str, Any]) -> str | None:
    """What is wrong with a reply as its schema has it, or with what the schema cannot
    say, or None. A build or a review replies in its own form and no other: the whole
    of the reply is held to the whole of the schema, here as where it is recorded."""
    if not isinstance(reply, dict):
        return "the reply is an object"
    said = schema_problem(reply, dict(flat))
    if said:
        return said
    if not all(str(text).strip() for text in reply.get("assumptions") or ()):
        return "an assumption says nothing"
    return None


def question_problem(reply: Mapping[str, Any]) -> str | None:
    asked = reply.get("question")
    if not isinstance(asked, dict):
        return "a question is an object with its text, why it matters and its options"
    if not str(asked.get("text", "")).strip() or not str(asked.get("why", "")).strip():
        return "a question needs its text and why it matters"
    if not all(str(option).strip() for option in asked.get("options", ())):
        return "an option of a question says nothing"
    if not all(str(text).strip() for text in reply.get("assumptions") or ()):
        return "an assumption says nothing"
    return None


def caller_of(invocation: Mapping[str, Any]) -> Principal:
    """The worker whose call this was: what it replied is done in its name."""
    role = str(invocation["role"])
    return Principal(PrincipalKind.WORKER, f"{role}@{invocation['worker_id']}", role)


async def work_of(connection: Connection, run: EffectRun) -> dict[str, Any]:
    row = await (
        await connection.execute(
            "SELECT * FROM assignments WHERE assignment_id = %s",
            (run.invocation["assignment_id"],),
        )
    ).fetchone()
    assert row is not None
    return row


async def record_assumptions(connection: Connection, run: EffectRun) -> None:
    work = await work_of(connection, run)
    for text in plain(run.invocation["reply"]).get("assumptions") or ():
        await assume_in(
            connection,
            caller_of(run.invocation),
            work["project_id"],
            work["feature_id"],
            story_id=work["story_id"],
            text=str(text),
        )


async def put_question(connection: Connection, run: EffectRun) -> None:
    """The question, what was assumed in asking it, and the end of this occasion,
    together: answering it owes the work again."""
    await record_assumptions(connection, run)
    work = await work_of(connection, run)
    asked = plain(run.invocation["reply"])["question"]
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


async def _nothing(run: EffectRun) -> None:
    """What this effect does, it does where it is marked done."""


ASK = Effect("ask", _nothing, put_question)


REVIEW_REPLY = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"enum": ["low", "medium", "high", "critical"]},
                    "claim": {"type": "string"},
                    "evidence": {"type": "string"},
                    "remediation": {"type": "string"},
                    "affected_files": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["severity", "claim", "evidence", "remediation"],
                "additionalProperties": False,
            },
        },
        "carried": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding_id": {"type": "string"},
                    "verdict": {"enum": ["verified_fixed", "still_present", "withdrawn"]},
                    "reason": {"type": "string"},
                },
                "required": ["finding_id", "verdict"],
                "additionalProperties": False,
            },
        },
        "question": QUESTION,
        "assumptions": _ASSUMPTIONS,
    },
    # A review, with its findings and how the earlier ones stand; or a question. One
    # form only: offered this and the form other kinds of work reply in, a real model
    # filled in both at once, and each such reply was a paid call refused.
    "required": [],
    "additionalProperties": False,
}


def _flat(schema: Mapping[str, Any]) -> dict[str, Any]:
    """A reply schema as its own flat form alone: what a reply is held to once read."""
    properties = {
        name: inner
        for name, inner in schema["properties"].items()
        if name not in ("outcome", "delivered")
    }
    return {**schema, "properties": properties}


@dataclass(frozen=True)
class Profile:
    """How a role's calls are made: which model, how hard it thinks, how long it has."""

    model: str
    effort: str | None = None
    timeout: float = 1800.0


class Workspaces:
    """Where runs work: one fresh directory each, under a root of Relay's own that is
    neither in the home directory nor in the directory every sandboxed shell shares."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def fresh(self) -> Path:
        return self._root / str(uuid.uuid7()) / "tree"


def review_from(reply: Any) -> Review:
    """The review a reply amounts to. Raises `SubmissionRefused` if it is not one."""
    if not isinstance(reply, dict):
        raise SubmissionRefused("a review is an object with findings and carried verdicts")
    try:
        findings = tuple(
            NewFinding(
                Severity[str(each["severity"]).upper()],
                str(each["claim"]),
                str(each["evidence"]),
                str(each["remediation"]),
                tuple(str(path) for path in each.get("affected_files", ())),
            )
            for each in reply["findings"]
        )
        carried = {str(each["finding_id"]): Carried(each["verdict"]) for each in reply["carried"]}
        reasons = {
            str(each["finding_id"]): str(each.get("reason", ""))
            for each in reply["carried"]
            if each["verdict"] == Carried.WITHDRAWN.value
        }
    except (KeyError, TypeError, ValueError) as malformed:
        raise SubmissionRefused(f"the review is not in the form asked for: {malformed!r}") from None
    return Review(findings, carried, reasons)


class ReviewTask:
    """QA and Sentinel: each reads its own export of the pinned candidate, and says what
    it found and how its earlier findings stand."""

    def __init__(
        self,
        pool: Pool,
        reviews: Reviews,
        invocations: Invocations,
        workspaces: Workspaces,
        definitions: Sequence[Path],
        profile: Profile,
    ) -> None:
        self._pool = pool
        self._reviews = reviews
        self._invocations = invocations
        self._workspaces = workspaces
        self._definitions = definitions
        self._profile = profile

    async def prepare(
        self, assignment: Mapping[str, Any], grant: uuid.UUID, last_problem: str | None
    ) -> Call:
        read_on = await self._invocations.as_it_stands(assignment["assignment_id"])
        about = await self._about(assignment["assignment_id"])
        workspace = self._workspaces.fresh()
        workspace.parent.mkdir(parents=True)
        # The checkout is the grant's first effect. Its receipt is recorded with the call:
        # what the model replies is a review of this tree and of no other.
        checkout = await self._reviews.prepare_review(about["task_id"], workspace, grant)
        role = str(assignment["role"])
        definition = find_definition(DEFAULT_DEFINITIONS[role], self._definitions)
        bounds = confinement_for(role, workspace)
        return Call(
            Prepared(
                provider="claude",
                model=self._profile.model,
                effort=self._profile.effort,
                definition=definition,
                effective_agent_sha256=effective_agent_sha256(definition, bounds.tools),
                briefing=_review_briefing(role, about, last_problem),
                schema=REVIEW_REPLY,
                read_on=read_on,
                evidence={"checkout": str(checkout.receipt)},
            ),
            workspace,
            bounds,
            self._profile.timeout,
        )

    def problem_with(self, reply: Any) -> str | None:
        shape = _shape_problem(reply, _flat(REVIEW_REPLY))
        if shape:
            return shape
        reply = plain(reply)
        if asks(reply):
            if "findings" in reply or "carried" in reply:
                return "a reply that asks has a question and no review"
            return question_problem(reply)
        if set(reply) - {"findings", "carried", "assumptions"}:
            return "a review has its findings, how the earlier ones stand, and nothing else"
        try:
            review_from(reply)
        except SubmissionRefused as refused:
            return str(refused)
        return None

    async def retain(
        self, assignment: Mapping[str, Any], call: Call, invocation_id: uuid.UUID
    ) -> str | None:
        return None  # a review's reply is all there is of it

    def discard(self, call: Call) -> None:
        shutil.rmtree(call.workdir.parent, ignore_errors=True)

    def effects(self, invocation: Mapping[str, Any]) -> list[Effect]:
        if asks(plain(invocation["reply"])):
            return [ASK]
        return [Effect("submit", self._submit)]

    async def _submit(self, run: EffectRun) -> Mapping[str, Any]:
        """Submit the review from the checkout the model was handed, under the grant that
        checkout was made under. Doing this again is doing it once: a review already
        accepted from the same content is answered with the receipt it has.

        A reply that has been adopted under another grant cannot be submitted: the
        review service takes a review only from a checkout made under the grant that
        holds the work, and this reply was written from another. That is nobody's
        defect. And only a submission the service accepts as the effective one is an
        applied review: one that arrives when the cycle has closed, or after another
        was accepted, is overtaken."""
        about = await self._about(run.invocation["assignment_id"])
        if run.grant_id != run.invocation["grant_id"]:
            raise Overtaken(
                "the review was written from a checkout made under a grant that no longer "
                "holds the work"
            )
        try:
            accepted = await self._reviews.submit_review(
                about["task_id"],
                review_from(plain(run.invocation["reply"])),
                run.grant_id,
                uuid.UUID(run.invocation["evidence"]["checkout"]),
                with_it=lambda connection: record_assumptions(connection, run),
            )
        except SubmissionRefused as refused:
            raise ActionRefused(str(refused)) from None
        if accepted.disposition != "effective":
            raise Overtaken(f"the review arrived {accepted.disposition}: it decides nothing")
        return {"submission_id": str(accepted.submission_id)}

    async def _about(self, assignment_id: uuid.UUID) -> dict[str, Any]:
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT t.task_id, t.role, c.project_id, c.candidate_revision, "
                    "(SELECT o.envelope->'payload'->>'reason' FROM assignments a JOIN outbox o "
                    "ON o.message_id = a.command_message_id "
                    "WHERE a.assignment_id = t.assignment_id) AS reason, "
                    "b.behaviour_id, "
                    "b.summary, b.contract, s.story_id, s.title, s.outcome, s.criteria "
                    "FROM review_tasks t JOIN review_cycles c USING (cycle_id) "
                    "JOIN behaviours b USING (project_id, behaviour_id) "
                    "JOIN stories s USING (project_id, story_id) WHERE t.assignment_id = %s",
                    (assignment_id,),
                )
            ).fetchone()
            if row is None:
                raise ActionRefused("there is no review behind this work")
            row["earlier"] = await (
                await connection.execute(
                    "SELECT f.finding_id, f.severity, f.claim, f.status FROM findings f "
                    "JOIN review_tasks t ON t.task_id = %s WHERE f.project_id = %s "
                    "AND f.behaviour_id = %s AND f.role = t.role AND f.status <> 'withdrawn' "
                    "ORDER BY f.finding_id",
                    (row["task_id"], row["project_id"], row["behaviour_id"]),
                )
            ).fetchall()
        return row


def _review_briefing(role: str, about: Mapping[str, Any], last_problem: str | None) -> str:
    what = {
        "qa": "the tests and the design of the tests: whether they assert what matters, are "
        "isolated, are free of flakiness, and cover the behaviour",
        "sentinel": "security: vulnerabilities, secrets, permissions, dependency risks, "
        "injection and unsafe execution",
    }[role]
    lines = [
        f"You are reviewing one candidate change for {what}.",
        "",
        (
            "The whole tree of the candidate is in your working directory. You can read it and "
            "search it. You cannot change it, and nothing you do there is kept: your reply is "
            "your review."
        ),
        "",
        f"The candidate is commit {about['candidate_revision']}.",
        f"It implements behaviour {about['behaviour_id']}: {about['summary']}",
        f"of story {about['story_id']}, {about['title']}: {about['outcome']}",
        "Acceptance criteria of the story:",
        *(f"  {name}: {text}" for name, text in sorted(about["criteria"].items())),
        "",
        (
            "The behaviour's acceptance contract, which Relay itself observes and which is "
            "not yours to judge the outcome of:"
        ),
        json.dumps(about["contract"], indent=2, sort_keys=True),
        "",
        (
            "Reply with your findings. For each: its severity (low, medium, high or critical), "
            "what is wrong, the evidence in the tree (file and line), and what would put it "
            "right. A finding is about this candidate as it is. Do not ask for coverage for "
            "its own sake, and do not report what you cannot point to."
        ),
    ]
    if about["earlier"]:
        lines += [
            "",
            (
                "Findings you raised on earlier candidates of this behaviour. For each, say how "
                "it stands in this candidate: verified_fixed, still_present, or withdrawn (with "
                "your reason)."
            ),
            *(
                f"  {each['finding_id']} (severity {Severity(each['severity']).name.lower()}): "
                f"{each['claim']}"
                for each in about["earlier"]
            ),
        ]
    else:
        lines += ["", "You have no earlier findings on this behaviour: reply with no carried ones."]
    lines += ["", ASKING_INSTEAD]
    if about.get("reason"):
        lines += ["", f"This review was owed before, and is owed again because {about['reason']}"]
    if last_problem:
        lines += ["", f"Your last reply could not be used: {last_problem}. Reply again in full."]
    return "\n".join(lines) + "\n"


class Tasks:
    """Which task carries out which kind of work, and how its known result is applied.
    A kind of work that differs by role (a reconfirmation is the Analyst's or the
    Verifier's) is registered under the kind and the role."""

    def __init__(self, by_kind: Mapping[Any, Any]) -> None:
        self.by_kind = dict(by_kind)

    def _for(self, kind: str, role: str) -> Any:
        return self.by_kind.get((kind, role)) or self.by_kind[kind]

    def effects_for(self, invocation: dict[str, Any]) -> Sequence[Effect]:
        """How this result is applied. A reply is read here too, whoever recorded it and
        with whatever they had to read it by: one its task would not have taken is
        applied as nothing, and that is its one effect."""
        task = self._for(invocation["kind"], invocation["role"])
        problem = task.problem_with(invocation["reply"])
        if problem is not None:

            async def refuse(run: EffectRun) -> None:
                raise ActionRefused(problem)

            return [Effect("read", refuse)]
        effects: Sequence[Effect] = task.effects(invocation)
        return effects

    def problem_with(self, kind: str, role: str, reply: Any) -> str | None:
        problem: str | None = self._for(kind, role).problem_with(reply)
        return problem

    def attach(self, invocations: Invocations) -> None:
        """Have this record apply results, and read replies, by these tasks."""
        invocations._effects_for = self.effects_for
        invocations.meaning_problem = self.problem_with


BUILD_REPLY = {
    "type": "object",
    "properties": {
        "note": {"type": "string"},
        "question": QUESTION,
        "assumptions": _ASSUMPTIONS,
    },
    # A note of what was built; or a question, and then nothing that was left is kept.
    "required": [],
    "additionalProperties": False,
}


class BuildTask:
    """The Builder: it is handed the tree to start from, changes it until the behaviour's
    acceptance contract would pass, and says what it did. It makes no commit, publishes
    nothing and judges nothing. The supervisor keeps its workspace as a commit, publishes
    that under a fresh attempt, and Relay itself observes whether it is green."""

    def __init__(
        self,
        pool: Pool,
        behaviours: Behaviours,
        invocations: Invocations,
        workspaces: Workspaces,
        mirrors: Path,
        definitions: Sequence[Path],
        profile: Profile,
        remote_timeout: float = 120.0,
    ) -> None:
        self._pool = pool
        self._behaviours = behaviours
        self._invocations = invocations
        self._workspaces = workspaces
        self._mirrors = mirrors
        self._definitions = definitions
        self._profile = profile
        self._remote_timeout = remote_timeout
        # The commit each workspace in use was exported from.
        self._bases: dict[Path, str] = {}

    def _mirror(self, project_id: str) -> Mirror:
        return Mirror(self._mirrors / f"{project_id}.git")

    async def prepare(
        self, assignment: Mapping[str, Any], grant: uuid.UUID, last_problem: str | None
    ) -> Call:
        read_on = await self._invocations.as_it_stands(assignment["assignment_id"])
        about = await self._about(assignment["assignment_id"])
        base = str(about["command"]["base_revision"])
        mirror = self._mirror(about["project_id"])
        # The red's revision for a build, the candidate being repaired for a remediation:
        # whichever the command recorded. It is on trunk or under one of the behaviour's
        # own attempt refs.
        if not await mirror.recover(
            about["repository"],
            base,
            [f"refs/heads/{about['trunk']}", *about["attempt_refs"]],
            timeout=self._remote_timeout,
        ):
            raise ActionRefused("the commit to build on can no longer be obtained")
        workspace = self._workspaces.fresh()
        workspace.parent.mkdir(parents=True)
        await mirror.export(base, workspace)
        self._bases[workspace] = base
        definition = find_definition(DEFAULT_DEFINITIONS["builder"], self._definitions)
        bounds = confinement_for("builder", workspace, withheld=[self._mirrors])
        return Call(
            Prepared(
                provider="claude",
                model=self._profile.model,
                effort=self._profile.effort,
                definition=definition,
                effective_agent_sha256=effective_agent_sha256(definition, bounds.tools),
                briefing=_build_briefing(about, last_problem),
                schema=BUILD_REPLY,
                read_on=read_on,
            ),
            workspace,
            bounds,
            self._profile.timeout,
        )

    def problem_with(self, reply: Any) -> str | None:
        shape = _shape_problem(reply, _flat(BUILD_REPLY))
        if shape:
            return shape
        reply = plain(reply)
        if asks(reply):
            if "note" in reply:
                return "a reply that asks has a question and no note of work done"
            return question_problem(reply)
        if not isinstance(reply.get("note"), str) or set(reply) - {"note", "assumptions"}:
            return "the reply is a note saying what was done"
        return None

    async def retain(
        self, assignment: Mapping[str, Any], call: Call, invocation_id: uuid.UUID
    ) -> str | None:
        """Keep the workspace as a commit on the base it was exported from, before the
        reply is recorded as usable: a result with no tree behind it is no result."""
        base = self._bases.pop(call.workdir)
        return await self._mirror(str(assignment["project_id"])).retain(
            call.workdir, base, f"refs/relay/invocations/{invocation_id}"
        )

    def discard(self, call: Call) -> None:
        self._bases.pop(call.workdir, None)
        shutil.rmtree(call.workdir.parent, ignore_errors=True)

    def effects(self, invocation: Mapping[str, Any]) -> list[Effect]:
        if asks(plain(invocation["reply"])):
            # Nothing was kept of what a call that asks left, and nothing is published.
            return [ASK]
        return [Effect("publish", self._publish), Effect("green", self._green)]

    async def _attempt(self, invocation: Mapping[str, Any]) -> int:
        """The attempt number of this call's candidate: one more than the behaviour has
        had, across builds and remediations, and the same however often it is asked."""
        about = await self._about(invocation["assignment_id"])
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute(
                "SELECT 1 FROM behaviours WHERE project_id = %s AND behaviour_id = %s FOR UPDATE",
                (about["project_id"], about["behaviour_id"]),
            )
            row = await (
                await connection.execute(
                    "INSERT INTO build_attempts (project_id, behaviour_id, attempt, invocation_id) "
                    "SELECT %s, %s, 1 + greatest("
                    "coalesce((SELECT max(attempt) FROM build_attempts WHERE project_id = %s "
                    "AND behaviour_id = %s), 0), "
                    "coalesce((SELECT max(g.attempt) FROM green_runs g JOIN assignments a "
                    "USING (assignment_id) WHERE a.project_id = %s AND a.behaviour_id = %s), 0)), "
                    "%s ON CONFLICT (invocation_id) DO UPDATE SET attempt = build_attempts.attempt "
                    "RETURNING attempt",
                    (
                        about["project_id"],
                        about["behaviour_id"],
                        about["project_id"],
                        about["behaviour_id"],
                        about["project_id"],
                        about["behaviour_id"],
                        invocation["invocation_id"],
                    ),
                )
            ).fetchone()
        assert row is not None
        return int(row["attempt"])

    async def _publish(self, run: EffectRun) -> Mapping[str, Any]:
        """Publish the kept commit under this call's attempt ref, with the supervisor's
        own access to the repository. An attempt ref already at that commit is done."""
        about = await self._about(run.invocation["assignment_id"])
        commit = str(run.invocation["retained"])
        attempt = await self._attempt(run.invocation)
        ref = attempt_ref(about["project_id"], about["behaviour_id"], attempt)
        try:
            there = await resolve_ref(about["repository"], ref, timeout=self._remote_timeout)
            if there is None:
                await self._mirror(about["project_id"]).publish(
                    about["repository"], commit, ref, timeout=self._remote_timeout
                )
            elif there != commit:
                raise ActionRefused(f"attempt {attempt} is already another candidate")
        except RemoteRejected as rejected:
            raise ActionRefused(f"the repository refused the candidate: {rejected}") from None
        return {"attempt": attempt, "ref": ref, "commit": commit}

    async def _green(self, run: EffectRun) -> Mapping[str, Any]:
        """Have Relay observe the candidate that was kept. A candidate that is not green
        is a reply that gave nothing: the next call is told what was observed.

        If this attempt has already been judged (the worker stopped after the verdict
        was recorded and before the effect was marked), the verdict that is on record
        is what is replayed, whichever way it went: nothing is observed twice, and a
        candidate that was found not green is not taken for one that was. What a try
        saw after it had been taken over is not a judgement, and is not replayed."""
        about = await self._about(run.invocation["assignment_id"])
        attempt = await self._attempt(run.invocation)
        async with self._pool.connection() as connection:
            judged = await (
                await connection.execute(
                    "SELECT event_type, payload->>'reason' AS reason FROM events "
                    "WHERE project_id = %s AND subject_id = %s AND event_type IN "
                    "('behaviour.green_verified', 'behaviour.green_rejected') "
                    "AND payload->>'attempt' = %s AND payload->>'candidate_revision' = %s "
                    # What a try that had been taken over saw is on record too, and is
                    # no verdict: only a run that was still the one to decide judged it.
                    "AND (payload->>'judged')::boolean ORDER BY sequence DESC LIMIT 1",
                    (
                        about["project_id"],
                        about["behaviour_id"],
                        str(attempt),
                        str(run.invocation["retained"]),
                    ),
                )
            ).fetchone()
        if judged is None:
            invocation_id, try_ = run.invocation["invocation_id"], run.attempt

            async def still_heard(connection: Connection) -> bool:
                # Asked where the verdict is accepted: a try that was taken over while
                # the oracle ran has its run kept, and opens no review.
                return await Invocations.is_current_try(connection, invocation_id, "green", try_)

            try:
                verdict = await self._behaviours.run_green(
                    about["project_id"],
                    about["behaviour_id"],
                    attempt,
                    run.grant_id,
                    # The commit that was kept and published, and no other the ref may name.
                    expecting=str(run.invocation["retained"]),
                    still_heard=still_heard,
                    with_it=lambda connection: record_assumptions(connection, run),
                )
            except CandidateMoved as moved:
                # Somebody moved the attempt ref after it was published, or removed it.
                # The reply is not at fault, and there is nothing of it left to observe.
                raise Overtaken(str(moved)) from None
            accepted, reason = verdict.is_valid, verdict.reason
        else:
            accepted, reason = judged["event_type"] == "behaviour.green_verified", judged["reason"]
        if not accepted:
            raise ActionRefused(f"the candidate is not green: {reason}")
        return {"attempt": attempt, "green": "accepted"}

    async def _about(self, assignment_id: uuid.UUID) -> dict[str, Any]:
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT a.project_id, a.kind, b.behaviour_id, b.summary, b.contract, "
                    "s.story_id, s.title, s.outcome, s.criteria, p.repository, p.trunk, "
                    "o.envelope->'payload' AS command FROM assignments a "
                    "JOIN behaviours b ON b.project_id = a.project_id "
                    "AND b.behaviour_id = a.behaviour_id "
                    "JOIN stories s ON s.project_id = b.project_id AND s.story_id = b.story_id "
                    "JOIN projects p ON p.project_id = a.project_id "
                    "JOIN outbox o ON o.message_id = a.command_message_id "
                    "WHERE a.assignment_id = %s",
                    (assignment_id,),
                )
            ).fetchone()
            if row is None:
                raise ActionRefused("there is no build behind this work")
            attempts = await (
                await connection.execute(
                    "SELECT attempt FROM build_attempts WHERE project_id = %s AND behaviour_id = %s",
                    (row["project_id"], row["behaviour_id"]),
                )
            ).fetchall()
            row["attempt_refs"] = [
                attempt_ref(row["project_id"], row["behaviour_id"], each["attempt"])
                for each in attempts
            ]
            row["findings"] = await (
                await connection.execute(
                    "SELECT finding_id, role, severity, claim, evidence, remediation FROM findings "
                    "WHERE project_id = %s AND behaviour_id = %s AND finding_id = ANY(%s) "
                    "ORDER BY finding_id",
                    (
                        row["project_id"],
                        row["behaviour_id"],
                        list(row["command"].get("remediate_findings", [])),
                    ),
                )
            ).fetchall()
        return row


def _build_briefing(about: Mapping[str, Any], last_problem: str | None) -> str:
    lines = [
        (
            "You are the Builder. Make the smallest change to the code in your working "
            "directory that makes one behaviour pass its acceptance contract."
        ),
        "",
        f"Behaviour {about['behaviour_id']}: {about['summary']}",
        f"of story {about['story_id']}, {about['title']}: {about['outcome']}",
        "Acceptance criteria of the story:",
        *(f"  {name}: {text}" for name, text in sorted(about["criteria"].items())),
        "",
        (
            "The acceptance contract. Relay launches exactly this, in a container, on what you "
            "leave in your working directory, and compares what it observes with what is "
            "expected. The contract is not yours to change: there is no file of it here."
        ),
        json.dumps(about["contract"], indent=2, sort_keys=True),
        "",
        (
            "Work test-first inside your own change where that helps. You have a shell with no "
            "network. Do not use Git: there is no repository here, and what you leave in this "
            "directory is what is kept, the whole tree of it. Leave nothing behind that is "
            "not part of the change."
        ),
        "",
        "When you are done, reply with a short note of what you changed and why.",
        "",
        ASKING_INSTEAD + " A reply that asks keeps nothing of what you left here.",
    ]
    if about["kind"] == "remediate":
        lines += ["", "This is a repair of a candidate that was sent back."]
        if about["findings"]:
            lines += [
                "The findings to put right:",
                *(
                    f"  {each['finding_id']} ({each['role']}, severity "
                    f"{Severity(each['severity']).name.lower()}): {each['claim']} "
                    f"Evidence: {each['evidence']} Remedy: {each['remediation']}"
                    for each in about["findings"]
                ),
            ]
        if about["command"].get("failed_checks"):
            lines += [
                "Checks every candidate must pass, which this one did not: "
                + ", ".join(about["command"]["failed_checks"])
            ]
    if about["command"].get("reason"):
        lines += [
            "",
            f"This work was owed before, and is owed again because {about['command']['reason']}",
        ]
    if last_problem:
        lines += ["", f"Your last attempt could not be used: {last_problem}"]
    return "\n".join(lines) + "\n"
