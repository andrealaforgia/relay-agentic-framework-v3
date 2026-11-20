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
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from relay.application.assignments import revoke_unstarted_grants
from relay.application.closure import CANCELLED, closure_of_feature, lock_gate
from relay.application.features import Features
from relay.application.stories import _record, _recorded, release_next
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
                receipt = Receipt("stale", f"nothing changed: the question is {question['state']}")
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
            await release_next(connection, project_id)
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
            if old["replaced"]:
                receipt = Receipt("stale", "nothing changed: that decision was already replaced")
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

    async def context_for(
        self, project_id: str, feature_id: str, story_id: str | None = None
    ) -> Context:
        """The decisions in force over a feature, or over one story of it, with the basis
        they amount to. Both are read in one transaction under the gate lock: a caller
        sees the whole of one state, never the text of one and the number of another."""
        async with self._pool.connection() as connection, connection.transaction():
            await lock_gate(connection, project_id)
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
                await basis_of(connection, project_id, feature_id, story_id),
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


async def basis_of(
    connection: Connection, project_id: str, feature_id: str, story_id: str | None = None
) -> int:
    """The number of the latest decision of the Product Owner's in force over the scope:
    feature-wide ones for a feature, those and its own for a story. Zero if none."""
    row = await (
        await connection.execute(
            "SELECT coalesce(max(number), 0) AS basis FROM decisions WHERE project_id = %s "
            "AND feature_id = %s AND source = 'product_owner' "
            "AND (scope_story = '' OR scope_story = %s)",
            (project_id, feature_id, story_id),
        )
    ).fetchone()
    assert row is not None
    return int(row["basis"])


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
    connection: Connection, project_id: str, feature_id: str, story_id: str | None
) -> None:
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
    if known["story_state"] == "approved":
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
