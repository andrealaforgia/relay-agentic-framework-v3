"""Questions for the Product Owner, and decisions.

A worker asks; only a person answers. While a question is open, nothing starts in its
scope, which is a whole feature or one story of it. Nothing is copied onto the things in
scope: wherever work starts, the open questions over it are looked up, so something
created after the question was asked is covered like everything else.

An answer is a decision of the Product Owner's. A worker may also record what a model
assumed. The two are kept apart wherever they are shown, and only a person's decision
moves the basis: the number that says which decisions a piece of work was made under.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from psycopg.types.json import Jsonb

from relay.application.assignments import (
    RECONFIRM,
    complete_reconfirmation,
    owe,
    retire_all_of_behaviour,
    retire_reconfirmations,
    revoke_unstarted_grants,
)
from relay.application.behaviours import CONTROL_PLANE, load_behaviour, save_behaviour
from relay.application.closure import (
    CANCELLED,
    closure_of_feature,
    feature_basis,
    lock_gate,
    require_basis,
    require_current,
    story_basis,
)
from relay.application.features import Features
from relay.application.stories import (
    _close_gate,
    _locked_project,
    _record,
    _recorded,
    lock_story,
    release_next,
    reopen_roadmap_gate,
    revisions_of_roadmap,
    why_a_proposal_cannot_stand,
)
from relay.domain.behaviour import BehaviourState, TransitionRefused
from relay.domain.evidence import ProcessContract
from relay.domain.story import (
    ON_A_DECISION,
    ON_A_QUESTION,
    ActionRefused,
    HumanAction,
    Principal,
    PrincipalKind,
    Receipt,
    answer_again,
    require_human,
    require_well_formed,
)
from relay.persistence.database import Connection, Pool
from relay.persistence.ledger import append_event
from relay.transport.envelope import Envelope, MessageKind

PRODUCT_OWNER = "product_owner"
MODEL = "model"


@dataclass(frozen=True)
class Question:
    question_id: uuid.UUID
    feature_id: str
    story_id: str | None
    asked_by: str
    text: str
    why: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class Decision:
    decision_id: uuid.UUID
    number: int
    source: str
    text: str
    story_id: str | None
    replaces: uuid.UUID | None


@dataclass(frozen=True)
class Context:
    """The decisions in force over a scope, and the basis they amount to. Read together:
    work written against these decisions names this basis when it is submitted."""

    basis: int
    decisions: tuple[Decision, ...]


class Decisions:
    def __init__(self, pool: Pool, features: Features) -> None:
        self._pool = pool
        self._features = features

    async def ask(
        self,
        principal: Principal,
        request_id: str,
        project_id: str,
        feature_id: str,
        *,
        story_id: str | None = None,
        text: str,
        why: str,
        options: Sequence[str],
    ) -> uuid.UUID:
        """Put a question to the Product Owner and stop its scope until it is answered.
        The principal and request id identify the request: asked again after a lost
        reply, it is the same question."""
        _require_worker(principal, "asks a question")
        options = tuple(options)
        if not text.strip() or not why.strip() or not all(option.strip() for option in options):
            raise ActionRefused("a question needs its text, why it matters and its options")
        scope = story_id or ""
        digest = _digest(
            {
                "project": project_id,
                "feature": feature_id,
                "story": scope,
                "text": text,
                "why": why,
                "options": list(options),
            }
        )
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id, exclusive=True)
            earlier = await (
                await connection.execute(
                    "SELECT question_id, digest FROM questions "
                    "WHERE asked_by = %s AND request_id = %s",
                    (principal.identity, request_id),
                )
            ).fetchone()
            if earlier is not None:
                if earlier["digest"] != digest:
                    raise ActionRefused(
                        f"request id {request_id!r} was already used for a different question"
                    )
                asked: uuid.UUID = earlier["question_id"]
                return asked
            await _require_scope(connection, project_id, feature_id, story_id)
            if await closure_of_feature(connection, project_id, feature_id) == CANCELLED:
                raise ActionRefused("the feature is cancelled: there is nothing to ask about")
            question_id = uuid.uuid7()
            await connection.execute(
                "INSERT INTO questions (question_id, project_id, feature_id, scope_story, "
                "asked_by, request_id, digest, text, why, options) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    question_id,
                    project_id,
                    feature_id,
                    scope,
                    principal.identity,
                    request_id,
                    digest,
                    text,
                    why,
                    Jsonb(list(options)),
                ),
            )
            await connection.execute(
                "INSERT INTO inbox_items (item_id, project_id, kind, question_id) "
                "VALUES (%s, %s, 'question', %s)",
                (uuid.uuid7(), project_id, question_id),
            )
            await revoke_unstarted_grants(connection, project_id, feature_id, story_id)
            await append_event(
                connection,
                project_id,
                "question.asked",
                subject_id=story_id or feature_id,
                payload={
                    "question_id": str(question_id),
                    "asked_by": principal.identity,
                    "text": text,
                    "why": why,
                    "options": list(options),
                },
            )
            return question_id

    async def answer(self, action: HumanAction) -> Receipt:
        """The Product Owner's answer: one decision, made once, and the question closed.
        What the question was holding is then taken up without anything else being asked."""
        require_human(action.principal)
        require_well_formed(action, ON_A_QUESTION)
        question_id = _as_id(action.target, "question")
        async with self._pool.connection() as connection:
            located = await (
                await connection.execute(
                    "SELECT project_id FROM questions WHERE question_id = %s", (question_id,)
                )
            ).fetchone()
        if located is None:
            raise ActionRefused("there is no such question")
        project_id: str = located["project_id"]
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id, exclusive=True)
            if (seen := answer_again(action, await _recorded(connection, action))) is not None:
                return seen
            question = await (
                await connection.execute(
                    "SELECT * FROM questions WHERE question_id = %s FOR UPDATE", (question_id,)
                )
            ).fetchone()
            assert question is not None
            if question["state"] != "open":
                receipt = Receipt("stale", await _how_it_ended(connection, question))
                await _record(connection, action, receipt)
                return receipt
            await connection.execute(
                "UPDATE questions SET state = 'answered' WHERE question_id = %s", (question_id,)
            )
            await connection.execute(
                "UPDATE inbox_items SET state = 'closed' WHERE question_id = %s", (question_id,)
            )
            decision_id = await _decide(
                connection,
                question,
                source=PRODUCT_OWNER,
                text=action.payload["text"],
                question_id=question_id,
            )
            await append_event(
                connection,
                project_id,
                "question.answered",
                subject_id=question["scope_story"] or question["feature_id"],
                payload={
                    "question_id": str(question_id),
                    "decision_id": str(decision_id),
                    "by": action.principal.identity,
                    "text": action.payload["text"],
                },
            )
            await _after_a_decision(connection, question)
            receipt = Receipt("effective", "answered")
            await _record(connection, action, receipt)
        await self._features.reconcile(project_id)
        return receipt

    async def replace(self, action: HumanAction) -> Receipt:
        """The Product Owner changes a decision, a model's included. The new one stands
        in the old one's scope; a decision already replaced is not replaced again."""
        require_human(action.principal)
        require_well_formed(action, ON_A_DECISION)
        decision_id = _as_id(action.target, "decision")
        async with self._pool.connection() as connection:
            located = await (
                await connection.execute(
                    "SELECT project_id FROM decisions WHERE decision_id = %s", (decision_id,)
                )
            ).fetchone()
        if located is None:
            raise ActionRefused("there is no such decision")
        project_id: str = located["project_id"]
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id, exclusive=True)
            if (seen := answer_again(action, await _recorded(connection, action))) is not None:
                return seen
            old = await (
                await connection.execute(
                    "SELECT d.*, EXISTS (SELECT 1 FROM decisions n WHERE n.replaces = "
                    "d.decision_id) AS replaced FROM decisions d WHERE d.decision_id = %s "
                    "FOR UPDATE OF d",
                    (decision_id,),
                )
            ).fetchone()
            assert old is not None
            pointless = (
                "that decision was already replaced"
                if old["replaced"]
                else await _beyond_changing(connection, old)
            )
            if pointless:
                receipt = Receipt("stale", f"nothing changed: {pointless}")
                await _record(connection, action, receipt)
                return receipt
            new_id = await _decide(
                connection,
                old,
                source=PRODUCT_OWNER,
                text=action.payload["text"],
                replaces=decision_id,
            )
            await append_event(
                connection,
                project_id,
                "decision.replaced",
                subject_id=old["scope_story"] or old["feature_id"],
                payload={
                    "decision_id": str(new_id),
                    "replaces": str(decision_id),
                    "by": action.principal.identity,
                    "text": action.payload["text"],
                },
            )
            await _after_a_decision(connection, old)
            receipt = Receipt("effective", "replaced")
            await _record(connection, action, receipt)
        await self._features.reconcile(project_id)
        return receipt

    async def assume(
        self,
        principal: Principal,
        project_id: str,
        feature_id: str,
        *,
        story_id: str | None = None,
        text: str,
    ) -> uuid.UUID:
        """Record what a model assumed, so a person can see it and overrule it. It stops
        nothing and moves no basis."""
        _require_worker(principal, "records an assumption")
        if not text.strip():
            raise ActionRefused("an assumption needs its text")
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
            # The project before the feature: the order every other transaction keeps.
            # And before the story is looked at: an approval holds this lock, and what
            # was true of the story before it commits is not what is true after.
            await _locked_project(connection, project_id)
            await _require_scope(connection, project_id, feature_id, story_id)
            scope = {
                "project_id": project_id,
                "feature_id": feature_id,
                "scope_story": story_id or "",
            }
            decision_id = await _decide(connection, scope, source=MODEL, text=text)
            await append_event(
                connection,
                project_id,
                "decision.assumed",
                subject_id=story_id or feature_id,
                payload={"decision_id": str(decision_id), "by": principal.identity, "text": text},
            )
            return decision_id

    async def evidence_of(self, project_id: str, behaviour_id: str) -> Evidence:
        """What a behaviour currently has on record, as the Verifier must name it when
        reconfirming the contract."""
        async with self._pool.connection() as connection:
            return await _evidence_of(connection, project_id, behaviour_id)

    async def reconfirm_roadmap(
        self,
        principal: Principal,
        project_id: str,
        feature_id: str,
        *,
        grant: uuid.UUID,
        basis: int,
        revision: int,
    ) -> None:
        """The Analyst states that one revision of the roadmap stands under the
        decisions now in force. The roadmap in force and a proposal still waiting are
        each owed separately. For the one in force, that lets its stories be released
        again. For a proposal, it puts the same stored proposal to the Product Owner
        again with those decisions shown; but a proposal that rewords or drops a story
        whose basis has moved cannot be made approvable this way, and has to be replaced."""
        _require_role(principal, "analyst")
        async with self._pool.connection() as connection, connection.transaction():
            # Exclusively, as every roadmap write is: reconfirming a proposal looks at
            # the stories it edits, which others lock in their own order.
            await lock_gate(connection, project_id, exclusive=True)
            await _locked_project(connection, project_id)
            statement = {"roadmap": feature_id, "basis": basis, "revision": revision}
            if not await _said_already(connection, project_id, grant, statement):
                require_basis(basis, await feature_basis(connection, project_id, feature_id))
                effective, proposed = await revisions_of_roadmap(connection, project_id, feature_id)
                if revision not in (effective, proposed):
                    raise ActionRefused(f"revision {revision} is not {feature_id}'s roadmap")
                if revision == proposed:
                    why = await why_a_proposal_cannot_stand(
                        connection, project_id, feature_id, revision
                    )
                    if why:
                        raise ActionRefused(f"{why}: the proposal has to be replaced")
                await complete_reconfirmation(
                    connection, grant, project_id, "roadmap", feature_id, revision
                )
                await connection.execute(
                    "UPDATE roadmaps SET current_on = %s WHERE project_id = %s "
                    "AND feature_id = %s AND revision = %s",
                    (basis, project_id, feature_id, revision),
                )
                await _reconfirmed(
                    connection, project_id, principal, grant, "roadmap", feature_id, basis,
                    statement,
                )  # fmt: skip
                if revision == proposed:
                    await reopen_roadmap_gate(connection, project_id, feature_id)
                await release_next(connection, project_id)
        await self._features.reconcile(project_id)

    async def reconfirm_story(
        self,
        principal: Principal,
        project_id: str,
        story_id: str,
        *,
        grant: uuid.UUID,
        basis: int,
        revision: int,
    ) -> None:
        """The Analyst states that the story's text and criteria stand under the
        decisions now in force."""
        _require_role(principal, "analyst")
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
            await lock_story(connection, project_id, story_id)
            await _locked_project(connection, project_id)
            statement = {"story": story_id, "basis": basis, "revision": revision}
            if not await _said_already(connection, project_id, grant, statement):
                require_basis(basis, await story_basis(connection, project_id, story_id))
                stood = await (
                    await connection.execute(
                        "UPDATE stories SET current_on = %s WHERE project_id = %s "
                        "AND story_id = %s AND revision = %s RETURNING story_id",
                        (basis, project_id, story_id, revision),
                    )
                ).fetchone()
                if stood is None:
                    raise ActionRefused(f"revision {revision} is not {story_id} as it stands")
                await complete_reconfirmation(connection, grant, project_id, "story", story_id)
                await _reconfirmed(
                    connection, project_id, principal, grant, "story", story_id, basis,
                    statement,
                )  # fmt: skip
                await release_next(connection, project_id)
        await self._features.reconcile(project_id)

    async def reconfirm_contract(
        self,
        principal: Principal,
        project_id: str,
        behaviour_id: str,
        *,
        grant: uuid.UUID,
        basis: int,
        contract_digest: str,
        evidence: Evidence,
    ) -> None:
        """The Verifier states that the contract stands under the decisions now in
        force, and so does every piece of evidence the behaviour has, named exactly.
        It is all of it or none: a Verifier who cannot vouch for all of it amends."""
        _require_role(principal, "verifier")
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
            row = await _locked_behaviour_row(connection, project_id, behaviour_id)
            await lock_story(connection, project_id, row["story_id"])
            await _locked_project(connection, project_id)
            statement = {
                "behaviour": behaviour_id,
                "basis": basis,
                "contract_digest": contract_digest,
                "retains": asdict(evidence),
            }
            if not await _said_already(connection, project_id, grant, statement):
                require_basis(basis, await story_basis(connection, project_id, row["story_id"]))
                behaviour = load_behaviour(row)
                if behaviour.contract is None or behaviour.contract.digest != contract_digest:
                    raise ActionRefused(f"that is not the contract {behaviour_id} has now")
                if evidence != await _evidence_of(connection, project_id, behaviour_id):
                    raise ActionRefused(
                        f"that is not the evidence {behaviour_id} has now: read it again"
                    )
                await complete_reconfirmation(
                    connection, grant, project_id, "behaviour", behaviour_id
                )
                await connection.execute(
                    "UPDATE behaviours SET current_on = %s "
                    "WHERE project_id = %s AND behaviour_id = %s",
                    (basis, project_id, behaviour_id),
                )
                await _reconfirmed(
                    connection, project_id, principal, grant, "behaviour", behaviour_id, basis,
                    statement,
                )  # fmt: skip
        await self._features.reconcile(project_id)

    async def amend_contract(
        self,
        principal: Principal,
        project_id: str,
        behaviour_id: str,
        contract: ProcessContract,
        *,
        grant: uuid.UUID,
        basis: int,
        replaces: str,
    ) -> None:
        """The Verifier replaces a contract that no longer stands. It is the other way
        of completing the reconfirmation owed for the behaviour, and needs the same
        grant. Everything gathered for the old contract is set aside: a fresh red first."""
        _require_role(principal, "verifier")
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
            row = await _locked_behaviour_row(connection, project_id, behaviour_id)
            behaviour = load_behaviour(row)
            statement = {
                "behaviour": behaviour_id,
                "basis": basis,
                "amends": replaces,
                "contract_digest": contract.digest,
            }
            if not await _said_already(connection, project_id, grant, statement):
                require_basis(basis, await story_basis(connection, project_id, row["story_id"]))
                await require_current(connection, project_id, row["story_id"])
                if behaviour.contract is None or behaviour.contract.digest != replaces:
                    raise ActionRefused(f"that is not the contract {behaviour_id} has now")
                if contract.digest == replaces:
                    # Otherwise this would be a way to send a candidate the repository
                    # refused round again without the Product Owner's retry.
                    raise ActionRefused(
                        "that is the same contract: an amendment changes it, and a "
                        "contract that stands is reconfirmed"
                    )
                set_aside = behaviour.state in (BehaviourState.REVIEW, BehaviourState.BLOCKED)
                try:
                    behaviour.amend_contract(contract)
                except TransitionRefused:
                    raise ActionRefused(
                        f"{behaviour_id} is {behaviour.state.value}: changing its contract "
                        "now needs a correction"
                    ) from None
                await complete_reconfirmation(
                    connection, grant, project_id, "behaviour", behaviour_id
                )
                await retire_all_of_behaviour(connection, project_id, behaviour_id)
                if set_aside:
                    # The candidate and its review were for the old contract. Findings
                    # are the behaviour's, not the cycle's: they stay as they stand,
                    # for the role that raised each to settle on a later candidate.
                    await connection.execute(
                        "UPDATE review_cycles SET state = 'superseded' WHERE project_id = %s "
                        "AND behaviour_id = %s AND state IN ('open', 'satisfied', 'blocked')",
                        (project_id, behaviour_id),
                    )
                await save_behaviour(connection, project_id, behaviour)
                await connection.execute(
                    "UPDATE behaviours SET current_on = %s, red_execution_id = NULL "
                    "WHERE project_id = %s AND behaviour_id = %s",
                    (basis, project_id, behaviour_id),
                )
                await append_event(
                    connection,
                    project_id,
                    "behaviour.contract_amended",
                    subject_id=behaviour_id,
                    payload={
                        "by": principal.identity,
                        "basis": basis,
                        "replaces": replaces,
                        "contract": contract.to_dict(),
                        "contract_digest": contract.digest,
                    },
                    correlation_id=row["correlation_id"],
                )
                await _reconfirmed(
                    connection, project_id, principal, grant, "behaviour", behaviour_id, basis,
                    statement, announce=False,
                )  # fmt: skip
        await self._features.reconcile(project_id)

    async def context_for(
        self, project_id: str, feature_id: str, story_id: str | None = None
    ) -> Context:
        """The decisions in force over a feature, or over one story of it, with the basis
        they amount to. Both are read in one transaction under the gate lock: a caller
        sees the whole of one state, never the text of one and the number of another."""
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
            await _require_scope(connection, project_id, feature_id, story_id, to_change=False)
            rows = await (
                await connection.execute(
                    "SELECT d.* FROM decisions d WHERE d.project_id = %s AND d.feature_id = %s "
                    "AND (d.scope_story = '' OR d.scope_story = %s) AND NOT EXISTS "
                    "(SELECT 1 FROM decisions n WHERE n.replaces = d.decision_id) "
                    "ORDER BY d.number",
                    (project_id, feature_id, story_id),
                )
            ).fetchall()
            return Context(
                await feature_basis(connection, project_id, feature_id)
                if story_id is None
                else await story_basis(connection, project_id, story_id),
                tuple(
                    Decision(
                        row["decision_id"],
                        row["number"],
                        row["source"],
                        row["text"],
                        row["scope_story"] or None,
                        row["replaces"],
                    )
                    for row in rows
                ),
            )

    async def open_questions(self, project_id: str) -> list[Question]:
        """What is waiting for the Product Owner's answer, read from the durable record."""
        async with self._pool.connection() as connection:
            rows = await (
                await connection.execute(
                    "SELECT q.* FROM inbox_items i JOIN questions q USING (question_id) "
                    "WHERE i.project_id = %s AND i.state = 'open' ORDER BY i.opened_at",
                    (project_id,),
                )
            ).fetchall()
        return [
            Question(
                row["question_id"],
                row["feature_id"],
                row["scope_story"] or None,
                row["asked_by"],
                row["text"],
                row["why"],
                tuple(row["options"]),
            )
            for row in rows
        ]


@dataclass(frozen=True)
class Evidence:
    """What a behaviour has on record, by identifier: the accepted red; the accepted
    green, the review cycle and the integration, where there is one."""

    red: str | None = None
    green: str | None = None
    cycle: str | None = None
    integration: str | None = None


async def _evidence_of(connection: Connection, project_id: str, behaviour_id: str) -> Evidence:
    row = await (
        await connection.execute(
            "SELECT b.red_execution_id::text AS red, c.green_execution_id::text AS green, "
            "c.cycle_id::text AS cycle, i.intent_id::text AS integration FROM behaviours b "
            "LEFT JOIN LATERAL (SELECT * FROM review_cycles c WHERE c.project_id = b.project_id "
            "AND c.behaviour_id = b.behaviour_id AND c.state IN ('open', 'satisfied', 'integrated') "
            "ORDER BY c.created_at DESC LIMIT 1) c ON TRUE "
            "LEFT JOIN integration_intents i ON i.cycle_id = c.cycle_id AND i.state = 'integrated' "
            "WHERE b.project_id = %s AND b.behaviour_id = %s",
            (project_id, behaviour_id),
        )
    ).fetchone()
    assert row is not None
    return Evidence(**row)


async def _after_a_decision(connection: Connection, scope: dict[str, Any]) -> None:
    """What a decision of the Product Owner's does to its scope, in its own transaction,
    under the exclusive gate lock: everything in scope was made under other decisions,
    so what a person was about to approve is withdrawn, seals are cleared, and each
    owner is asked to reconfirm or change what is theirs. Nothing can be released by it:
    whatever it touches now waits for those owners.

    The reconfirmations and the amendment need no check of their own that the scope is
    open. A pause or a question takes back every grant that has done nothing, and these
    grants do nothing until the moment they complete."""
    project_id, feature_id = scope["project_id"], scope["feature_id"]
    story_scope: str | None = scope["scope_story"] or None
    stories = await (
        await connection.execute(
            "SELECT story_id, story_basis(project_id, story_id) AS basis FROM stories "
            "WHERE project_id = %s AND feature_id = %s AND (%s::text IS NULL OR story_id = %s) "
            "AND state NOT IN ('approved', 'rejected', 'cancelled', 'withdrawn') "
            "ORDER BY position FOR UPDATE",
            (project_id, feature_id, story_scope, story_scope),
        )
    ).fetchall()
    in_scope = [story["story_id"] for story in stories]
    await _locked_project(connection, project_id)
    gates = await (
        await connection.execute(
            "SELECT * FROM gates WHERE project_id = %s AND feature_id = %s AND state = 'open' "
            "AND ((kind = 'roadmap' AND %s::text IS NULL) OR story_id = ANY(%s)) FOR UPDATE",
            (project_id, feature_id, story_scope, in_scope),
        )
    ).fetchall()
    for gate in gates:
        await _close_gate(connection, gate, "superseded", "superseded")
    await connection.execute(
        "UPDATE stories SET seal_id = NULL, sealed_revision = NULL, state = CASE "
        "WHEN state = 'awaiting_approval' THEN 'released' ELSE state END "
        "WHERE project_id = %s AND story_id = ANY(%s)",
        (project_id, in_scope),
    )
    await revoke_unstarted_grants(connection, project_id, feature_id, story_scope)
    await retire_reconfirmations(connection, project_id, feature_id, story_scope)
    if story_scope is None:
        # The roadmap in force and a proposal still waiting are separate things to stand
        # behind, and each is owed on its own.
        for revision in await revisions_of_roadmap(connection, project_id, feature_id):
            if revision is not None:
                await _owe_reconfirmation(
                    connection,
                    project_id,
                    feature_id,
                    "analyst",
                    await feature_basis(connection, project_id, feature_id),
                    revision=revision,
                )
    for story in stories:
        await _owe_reconfirmation(
            connection,
            project_id,
            feature_id,
            "analyst",
            story["basis"],
            story_id=story["story_id"],
        )
        contracts = await (
            await connection.execute(
                "SELECT behaviour_id, correlation_id FROM behaviours WHERE project_id = %s "
                "AND story_id = %s AND contract IS NOT NULL ORDER BY position",
                (project_id, story["story_id"]),
            )
        ).fetchall()
        for behaviour in contracts:
            await _owe_reconfirmation(
                connection,
                project_id,
                feature_id,
                "verifier",
                story["basis"],
                story_id=story["story_id"],
                behaviour_id=behaviour["behaviour_id"],
            )


async def _owe_reconfirmation(
    connection: Connection,
    project_id: str,
    feature_id: str,
    role: str,
    basis: int,
    *,
    story_id: str | None = None,
    behaviour_id: str | None = None,
    revision: int | None = None,
) -> None:
    subject = behaviour_id or story_id or f"{feature_id}@{revision}"
    await owe(
        connection,
        RECONFIRM,
        Envelope(
            message_kind=MessageKind.COMMAND,
            event_type="reconfirmation.requested",
            project_id=project_id,
            sender=CONTROL_PLANE,
            recipient=role,
            idempotency_key=f"reconfirm:{project_id}:{subject}:{basis}",
            correlation_id=uuid.uuid7(),
            story_id=story_id if behaviour_id is None else None,
            behaviour_id=behaviour_id,
            payload={"basis": basis, "feature_id": feature_id},
        ),
        issued_on={"basis": basis} | ({"revision": revision} if revision is not None else {}),
        feature_id=feature_id,
    )


async def _said_already(
    connection: Connection,
    project_id: str,
    grant: uuid.UUID,
    statement: dict[str, Any],
) -> bool:
    """Whether this grant has already completed exactly this, in this project. A caller
    cut off after the commit may ask again and is acknowledged; the follow-up is then run
    again. A grant that completed anything else acknowledges nothing: it is refused."""
    done = await (
        await connection.execute(
            "SELECT statement FROM reconfirmations WHERE grant_id = %s AND project_id = %s",
            (grant, project_id),
        )
    ).fetchone()
    if done is None:
        return False
    if done["statement"] != statement:
        raise ActionRefused("this grant was used for something else, and for that only")
    return True


async def _how_it_ended(connection: Connection, question: dict[str, Any]) -> str:
    """What a late answer is told: which decision stands, if the question was answered."""
    decision = await (
        await connection.execute(
            "SELECT decision_id FROM decisions WHERE question_id = %s", (question["question_id"],)
        )
    ).fetchone()
    if decision is None:
        return f"nothing changed: the question is {question['state']}"
    return (
        f"nothing changed: the question was already answered by decision {decision['decision_id']}"
    )


async def _beyond_changing(connection: Connection, decision: dict[str, Any]) -> str:
    """Why replacing this decision would change nothing, or an empty string. A decision
    of a cancelled feature, or one about a story already approved, has nothing left to
    govern: replacing it would only owe work nobody can use."""
    row = await (
        await connection.execute(
            "SELECT f.closed_by, (SELECT s.state FROM stories s WHERE s.project_id = f.project_id "
            "AND s.feature_id = f.feature_id AND s.story_id = %s) AS story_state "
            "FROM features f WHERE f.project_id = %s AND f.feature_id = %s",
            (decision["scope_story"], decision["project_id"], decision["feature_id"]),
        )
    ).fetchone()
    assert row is not None
    if row["closed_by"] == CANCELLED:
        return "the feature is cancelled"
    if row["story_state"] == "approved":
        return f"{decision['scope_story']} is approved, and changing it is a correction"
    return ""


async def _reconfirmed(
    connection: Connection,
    project_id: str,
    principal: Principal,
    grant: uuid.UUID,
    subject: str,
    subject_id: str,
    basis: int,
    statement: dict[str, Any],
    *,
    announce: bool = True,
) -> None:
    """Record what a grant completed, exactly as it was submitted. An amendment is
    recorded here too, with its own event already in the ledger."""
    await connection.execute(
        "INSERT INTO reconfirmations (reconfirmation_id, project_id, subject, subject_id, basis, "
        "by, grant_id, statement) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            uuid.uuid7(),
            project_id,
            subject,
            subject_id,
            basis,
            principal.identity,
            grant,
            Jsonb(statement),
        ),
    )
    if not announce:
        return
    await append_event(
        connection,
        project_id,
        f"{subject}.reconfirmed",
        subject_id=subject_id,
        payload={"by": principal.identity, "basis": basis, **statement},
    )


async def _locked_behaviour_row(
    connection: Connection, project_id: str, behaviour_id: str
) -> dict[str, Any]:
    row = await (
        await connection.execute(
            "SELECT * FROM behaviours WHERE project_id = %s AND behaviour_id = %s FOR UPDATE",
            (project_id, behaviour_id),
        )
    ).fetchone()
    if row is None:
        raise ActionRefused(f"unknown behaviour {behaviour_id!r}")
    return row


def _require_role(principal: Principal, role: str) -> None:
    if principal.kind is not PrincipalKind.WORKER or principal.role != role:
        raise ActionRefused(f"this is for the {role} to do")


async def _decide(
    connection: Connection,
    scope: dict[str, Any],
    *,
    source: str,
    text: str,
    question_id: uuid.UUID | None = None,
    replaces: uuid.UUID | None = None,
) -> uuid.UUID:
    """Record a decision in the scope given, numbered next in its feature. Called under
    the gate lock, shared or exclusive; the feature's row is locked to number it."""
    project_id, feature_id = scope["project_id"], scope["feature_id"]
    await connection.execute(
        "SELECT 1 FROM features WHERE project_id = %s AND feature_id = %s FOR UPDATE",
        (project_id, feature_id),
    )
    decision_id = uuid.uuid7()
    await connection.execute(
        "INSERT INTO decisions (decision_id, project_id, feature_id, scope_story, number, "
        "source, text, question_id, replaces) SELECT %s, %s, %s, %s, coalesce(max(number), 0) + 1, "
        "%s, %s, %s, %s FROM decisions WHERE project_id = %s AND feature_id = %s",
        (
            decision_id,
            project_id,
            feature_id,
            scope["scope_story"],
            source,
            text,
            question_id,
            replaces,
            project_id,
            feature_id,
        ),
    )
    return decision_id


async def _require_scope(
    connection: Connection,
    project_id: str,
    feature_id: str,
    story_id: str | None,
    *,
    to_change: bool = True,
) -> None:
    """The feature exists, and the story named is one of its own. To ask or assume
    something about a story it must also not be approved yet; reading its context is
    always allowed."""
    known = await (
        await connection.execute(
            "SELECT (SELECT s.state FROM stories s WHERE s.project_id = f.project_id "
            "AND s.feature_id = f.feature_id AND s.story_id = %s) AS story_state "
            "FROM features f WHERE f.project_id = %s AND f.feature_id = %s",
            (story_id, project_id, feature_id),
        )
    ).fetchone()
    if known is None:
        raise ActionRefused(f"unknown feature {feature_id!r}")
    if story_id is not None and known["story_state"] is None:
        raise ActionRefused(f"{story_id!r} is not a story of {feature_id!r}")
    if to_change and known["story_state"] in ("rejected", "withdrawn"):
        raise ActionRefused(
            f"{story_id} is {known['story_state']}: it is history, and nothing more is "
            "asked or assumed about it"
        )
    if to_change and known["story_state"] == "approved":
        raise ActionRefused(f"{story_id} is approved: changing it is a correction")


def _require_worker(principal: Principal, what: str) -> None:
    if principal.kind is not PrincipalKind.WORKER:
        raise ActionRefused(f"a worker {what}; a person answers and decides")


def _as_id(target: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(target)
    except ValueError:
        raise ActionRefused(f"there is no such {what}") from None


def _digest(document: dict[str, Any]) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
