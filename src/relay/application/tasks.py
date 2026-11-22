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

from relay.application.invocations import Effect, EffectRun, Invocations, Prepared
from relay.application.reviews import NewFinding, Review, Reviews
from relay.application.worker import Call
from relay.domain.review import Carried, Severity, SubmissionRefused
from relay.domain.story import ActionRefused
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
