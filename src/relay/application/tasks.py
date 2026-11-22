"""The kinds of work a worker carries out, each as: what the model is briefed with, what
it may reply, and what trusted code then does with the reply.

A role's restriction is not left to the prose of its agent definition. It is the shape
of the reply: a reviewer's reply has no field through which a file could be changed, and
its run has no tool that could change one.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relay.application.behaviours import Behaviours
from relay.application.invocations import Effect, EffectRun, Invocations, Prepared
from relay.application.reviews import NewFinding, Review, Reviews
from relay.application.worker import Call
from relay.domain.review import Carried, Severity, SubmissionRefused
from relay.domain.story import ActionRefused
from relay.git.checkout import RemoteRejected, attempt_ref, resolve_ref
from relay.git.mirror import Mirror
from relay.persistence.database import Pool
from relay.providers.confinement import confinement_for, effective_agent_sha256
from relay.providers.definitions import DEFAULT_DEFINITIONS, find_definition

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
    },
    "required": ["findings", "carried"],
    "additionalProperties": False,
}


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
        # The checkout is the grant's first effect, and its receipt is what the review
        # will be submitted from.
        await self._reviews.prepare_review(about["capability"], workspace, grant)
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
            ),
            workspace,
            bounds,
            self._profile.timeout,
        )

    def problem_with(self, reply: Any) -> str | None:
        try:
            review_from(reply)
        except SubmissionRefused as refused:
            return str(refused)
        return None

    async def retain(
        self, assignment: Mapping[str, Any], call: Call, invocation_id: uuid.UUID
    ) -> str | None:
        return None  # a review's reply is all there is of it

    def effects(self, invocation: Mapping[str, Any]) -> list[Effect]:
        return [Effect("submit", self._submit)]

    async def _submit(self, run: EffectRun) -> Mapping[str, Any]:
        """Submit the review under the grant that holds the work, from a checkout made
        under that grant. Doing this again is doing it once: a review already accepted
        from the same content is answered with the receipt it has."""
        about = await self._about(run.invocation["assignment_id"])
        async with self._pool.connection() as connection:
            held = await (
                await connection.execute(
                    "SELECT receipt_id FROM review_checkouts WHERE task_id = %s AND grant_id = %s "
                    "ORDER BY prepared_at DESC, receipt_id DESC LIMIT 1",
                    (about["task_id"], run.grant_id),
                )
            ).fetchone()
        try:
            if held is None:
                # The result was adopted under a grant that has no checkout of its own.
                # It makes one, of the same pinned commit, and discards the tree.
                scratch = self._workspaces.fresh()
                scratch.parent.mkdir(parents=True)
                receipt = (
                    await self._reviews.prepare_review(about["capability"], scratch, run.grant_id)
                ).receipt
            else:
                receipt = held["receipt_id"]
            accepted = await self._reviews.submit_review(
                about["capability"], review_from(run.invocation["reply"]), run.grant_id, receipt
            )
        except SubmissionRefused as refused:
            raise ActionRefused(str(refused)) from None
        return {"submission_id": str(accepted.submission_id), "disposition": accepted.disposition}

    async def _about(self, assignment_id: uuid.UUID) -> dict[str, Any]:
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT t.task_id, t.capability, t.role, c.candidate_revision, b.behaviour_id, "
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
                    "JOIN review_tasks t ON t.task_id = %s WHERE f.behaviour_id = %s "
                    "AND f.role = t.role AND f.status <> 'withdrawn' ORDER BY f.finding_id",
                    (row["task_id"], row["behaviour_id"]),
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
    if last_problem:
        lines += ["", f"Your last reply could not be used: {last_problem}. Reply again in full."]
    return "\n".join(lines) + "\n"


class Tasks:
    """Which task carries out which kind of work, and how its known result is applied."""

    def __init__(self, by_kind: Mapping[str, Any]) -> None:
        self.by_kind = dict(by_kind)

    def effects_for(self, invocation: dict[str, Any]) -> Sequence[Effect]:
        effects: Sequence[Effect] = self.by_kind[invocation["kind"]].effects(invocation)
        return effects


BUILD_REPLY = {
    "type": "object",
    "properties": {"note": {"type": "string"}},
    "required": ["note"],
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
        if not isinstance(reply, dict) or not isinstance(reply.get("note"), str):
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

    def effects(self, invocation: Mapping[str, Any]) -> list[Effect]:
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
        """Have Relay observe the candidate. A candidate that is not green is a reply
        that gave nothing: the next call is told what was observed."""
        about = await self._about(run.invocation["assignment_id"])
        attempt = await self._attempt(run.invocation)
        async with self._pool.connection() as connection:
            judged = await (
                await connection.execute(
                    "SELECT 1 FROM green_runs WHERE assignment_id = %s AND attempt = %s "
                    "AND state = 'finished'",
                    (run.invocation["assignment_id"], attempt),
                )
            ).fetchone()
        if judged is not None:
            return {"attempt": attempt, "green": "already judged"}
        verdict = await self._behaviours.run_green(
            about["project_id"], about["behaviour_id"], attempt, run.grant_id
        )
        if not verdict.is_valid:
            raise ActionRefused(f"the candidate is not green: {verdict.reason}")
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
    if last_problem:
        lines += ["", f"Your last attempt could not be used: {last_problem}"]
    return "\n".join(lines) + "\n"
