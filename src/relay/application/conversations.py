"""The Product Owner's conversation with Relay (PO-3), as Relay records it.

A conversation is Relay's, not the provider's. Each turn is a paid call with an identity
of its own, recorded with the person's words before it is made and settled once (3C-3.6).
What the Interpreter works out is kept as versions of an analysis and as notes of
questions it means to ask (3C-2.9); the person's own words are kept against the note
they answered (3C-3.7). None of it decides anything. A session that is closed, or whose
provider session is lost, leaves all of it: `context` is what a new one is given (3C-3.8).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from relay.application.inbox import items_in
from relay.persistence.database import Pool

ROLE = "interpreter"
WRITTEN_BY = "written by a model, not by the Product Owner"


class NotThisConversation(LookupError):
    """What was named is not there, or is another project's."""


@dataclass(frozen=True)
class Turn:
    turn_id: uuid.UUID
    number: int


async def open_conversation(pool: Pool, project_id: str) -> uuid.UUID:
    conversation = uuid.uuid7()
    async with pool.connection() as connection:
        await connection.execute(
            "INSERT INTO conversations (conversation_id, project_id) VALUES (%s, %s)",
            (conversation, project_id),
        )
    return conversation


async def close_conversation(pool: Pool, conversation: uuid.UUID) -> None:
    async with pool.connection() as connection:
        await connection.execute(
            "UPDATE conversations SET closed_at = clock_timestamp() "
            "WHERE conversation_id = %s AND closed_at IS NULL",
            (conversation,),
        )


async def provider_session_of(pool: Pool, conversation: uuid.UUID) -> uuid.UUID | None:
    async with pool.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT provider_session FROM conversations WHERE conversation_id = %s",
                (conversation,),
            )
        ).fetchone()
    if row is None:
        raise NotThisConversation(f"there is no conversation {conversation}")
    return row["provider_session"]  # type: ignore[no-any-return]


async def begin_turn(pool: Pool, conversation: uuid.UUID, said: str, model: str) -> Turn:
    """Record a turn before its call is made: the person's words, as typed, and the call's
    own fresh identity. Whatever happens to the call, what the person said is on record."""
    turn = uuid.uuid7()
    async with pool.connection() as connection, connection.transaction():
        of = await (
            await connection.execute(
                "SELECT project_id FROM conversations WHERE conversation_id = %s FOR UPDATE",
                (conversation,),
            )
        ).fetchone()
        if of is None:
            raise NotThisConversation(f"there is no conversation {conversation}")
        row = await (
            await connection.execute(
                "INSERT INTO conversation_turns (turn_id, conversation_id, project_id, number, "
                "said, model) SELECT %s, %s, %s, coalesce(max(number), 0) + 1, %s, %s "
                "FROM conversation_turns WHERE conversation_id = %s RETURNING number",
                (turn, conversation, of["project_id"], said, model, conversation),
            )
        ).fetchone()
    assert row is not None
    return Turn(turn, int(row["number"]))


async def settle_turn(
    pool: Pool,
    turn: uuid.UUID,
    state: str,
    *,
    reply: str | None = None,
    reason: str | None = None,
    continued: bool | None = None,
    provider_session: uuid.UUID | None = None,
    usage: Sequence[dict[str, Any]] | None = None,
    provider_cost_usd: Decimal | None = None,
    estimated_cost_usd: Decimal | None = None,
    price_table: str | None = None,
    envelope: dict[str, Any] | None = None,
) -> bool:
    """Settle a turn, once: a turn that is settled already is left as it was settled.
    A turn whose result named its provider session leaves that with the conversation for
    the next turn to continue, however the turn went: a session that turns out not to be
    there is found out when it is next asked for, and a new one made."""
    async with pool.connection() as connection, connection.transaction():
        settled = await (
            await connection.execute(
                "UPDATE conversation_turns SET state = %s, reply = %s, reason = %s, "
                "continued = %s, provider_session = %s, usage = %s, provider_cost_usd = %s, "
                "estimated_cost_usd = %s, price_table = %s, envelope = %s, "
                "ended_at = clock_timestamp() WHERE turn_id = %s AND state = 'intended' "
                "RETURNING conversation_id",
                (
                    state,
                    reply,
                    reason,
                    continued,
                    provider_session,
                    Jsonb(list(usage)) if usage is not None else None,
                    provider_cost_usd,
                    estimated_cost_usd,
                    price_table,
                    Jsonb(envelope) if envelope is not None else None,
                    turn,
                ),
            )
        ).fetchone()
        if settled is None:
            return False
        if provider_session is not None:
            await connection.execute(
                "UPDATE conversations SET provider_session = %s WHERE conversation_id = %s",
                (provider_session, settled["conversation_id"]),
            )
    return True


async def give_up_on_turns_left_open(pool: Pool, project_id: str, older_than: float) -> int:
    """A turn whose host died before settling it: nobody knows how its call ended. Said
    to be so once it is older than any call is allowed to run."""
    async with pool.connection() as connection:
        swept = await connection.execute(
            "UPDATE conversation_turns SET state = 'uncertain', ended_at = clock_timestamp(), "
            "reason = 'the session ended before this turn was settled' "
            "WHERE project_id = %s AND state = 'intended' "
            "AND started_at < clock_timestamp() - make_interval(secs => %s)",
            (project_id, older_than),
        )
    return swept.rowcount


async def _feature_of(connection: Any, project_id: str, feature_id: str | None) -> None:
    if feature_id is None:
        return
    there = await (
        await connection.execute(
            "SELECT 1 FROM features WHERE project_id = %s AND feature_id = %s",
            (project_id, feature_id),
        )
    ).fetchone()
    if there is None:
        raise NotThisConversation(f"there is no feature {feature_id!r} in {project_id}")


async def record_analysis(
    pool: Pool, conversation: uuid.UUID, text: str, feature_id: str | None = None
) -> int:
    """A new version of the Interpreter's analysis. It decides nothing."""
    if not text.strip():
        raise ValueError("an analysis says something")
    async with pool.connection() as connection, connection.transaction():
        of = await _project_of(connection, conversation)
        await _feature_of(connection, of, feature_id)
        row = await (
            await connection.execute(
                "INSERT INTO analyses (project_id, version, feature_id, text, conversation_id) "
                "SELECT %s, coalesce(max(version), 0) + 1, %s, %s, %s FROM analyses "
                "WHERE project_id = %s RETURNING version",
                (of, feature_id, text.strip(), conversation, of),
            )
        ).fetchone()
    assert row is not None
    return int(row["version"])


async def analysis_of(pool: Pool, project_id: str, version: int) -> dict[str, Any] | None:
    async with pool.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT version, feature_id, text, recorded_at FROM analyses "
                "WHERE project_id = %s AND version = %s",
                (project_id, version),
            )
        ).fetchone()
    return dict(row) if row else None


async def note_question(
    pool: Pool, conversation: uuid.UUID, text: str, feature_id: str | None = None
) -> uuid.UUID:
    """A question the Interpreter means to put to the person. Not a workflow question."""
    if not text.strip():
        raise ValueError("a note of a question has the question in it")
    note = uuid.uuid7()
    async with pool.connection() as connection, connection.transaction():
        of = await _project_of(connection, conversation)
        await _feature_of(connection, of, feature_id)
        await connection.execute(
            "INSERT INTO notes (note_id, project_id, feature_id, text, conversation_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (note, of, feature_id, text.strip(), conversation),
        )
    return note


async def note_answered(pool: Pool, conversation: uuid.UUID, note: str) -> str:
    """The Interpreter says which note the person's last words answered. What is recorded
    against the note is what the person typed in the turn being made, as typed: the model
    names the note, and has no say in the words. Returns those words."""
    async with pool.connection() as connection, connection.transaction():
        of = await _project_of(connection, conversation)
        turn = await (
            await connection.execute(
                "SELECT turn_id, said FROM conversation_turns WHERE conversation_id = %s "
                "ORDER BY number DESC LIMIT 1",
                (conversation,),
            )
        ).fetchone()
        if turn is None:
            raise NotThisConversation("nothing has been said in this conversation yet")
        found = await (
            await connection.execute(
                "SELECT note_id, answered_in FROM notes WHERE project_id = %s "
                "AND note_id::text LIKE %s || '%%' FOR UPDATE",
                (of, note.strip().lower()),
            )
        ).fetchall()
        if len(found) != 1:
            raise NotThisConversation(
                f"no one note of {of} is {note!r}: `context` lists them with their identities"
            )
        if found[0]["answered_in"] is not None:
            raise NotThisConversation(f"the note {note!r} has its answer already")
        await connection.execute(
            "UPDATE notes SET answered_in = %s, answer = %s, answered_at = clock_timestamp() "
            "WHERE note_id = %s",
            (turn["turn_id"], turn["said"], found[0]["note_id"]),
        )
    return str(turn["said"])


async def _project_of(connection: Any, conversation: uuid.UUID) -> str:
    row = await (
        await connection.execute(
            "SELECT project_id FROM conversations WHERE conversation_id = %s", (conversation,)
        )
    ).fetchone()
    if row is None:
        raise NotThisConversation(f"there is no conversation {conversation}")
    return str(row["project_id"])


async def context(pool: Pool, project_id: str) -> dict[str, Any]:
    """What a session needs to carry on, from Relay's record and from nothing that was only
    in an earlier session's transcript (3C-1.11, 3C-2.9, 3C-3.7)."""
    from relay.cli.reading import item_facts

    async with pool.connection() as connection, connection.transaction():
        await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        features = await (
            await connection.execute(
                "SELECT feature_id, problem, state FROM features WHERE project_id = %s "
                "ORDER BY feature_id",
                (project_id,),
            )
        ).fetchall()
        decisions = await (
            await connection.execute(
                "SELECT d.feature_id, d.scope_story, d.number, d.source, d.text FROM decisions d "
                "WHERE d.project_id = %s AND NOT EXISTS "
                "(SELECT 1 FROM decisions n WHERE n.replaces = d.decision_id) "
                "ORDER BY d.feature_id, d.number",
                (project_id,),
            )
        ).fetchall()
        questions = await (
            await connection.execute(
                "SELECT question_id, feature_id, scope_story, asked_by, text, why FROM questions "
                "WHERE project_id = %s AND state = 'open' ORDER BY asked_at",
                (project_id,),
            )
        ).fetchall()
        waiting = await items_in(connection, project_id)
        prepared = await (
            await connection.execute(
                "SELECT code, verb, target, said, reason, why, prepared_by, prepared_at, "
                "expires_at FROM prepared_actions WHERE project_id = %s AND ended IS NULL "
                "AND consumed_by IS NULL AND expires_at > now() ORDER BY prepared_at",
                (project_id,),
            )
        ).fetchall()
        analysis = await (
            await connection.execute(
                "SELECT version, feature_id, text, recorded_at FROM analyses "
                "WHERE project_id = %s ORDER BY version DESC LIMIT 1",
                (project_id,),
            )
        ).fetchone()
        notes = await (
            await connection.execute(
                "SELECT n.note_id, n.feature_id, n.text, n.noted_at, n.answer, n.answered_at, "
                # What the person has typed since an open note was made: theirs, on
                # record before any call, whether or not a model got to read it.
                "(SELECT coalesce(jsonb_agg(t.said ORDER BY t.started_at), '[]'::jsonb) "
                "FROM conversation_turns t WHERE t.project_id = n.project_id "
                "AND t.started_at > n.noted_at) AS said_since "
                "FROM notes n WHERE n.project_id = %s ORDER BY n.noted_at",
                (project_id,),
            )
        ).fetchall()
    return {
        "project": project_id,
        "features": [
            {"feature": each["feature_id"], "state": each["state"], "problem": each["problem"]}
            for each in features
        ],
        "decisions_in_force": [
            {
                "feature": each["feature_id"],
                "story": each["scope_story"] or None,
                "decision": f"D{each['number']}",
                "made_by": "the Product Owner" if each["source"] == "product_owner" else "a model",
                "text": each["text"],
            }
            for each in decisions
        ],
        "open_questions": [
            {
                "question": str(each["question_id"]),
                "feature": each["feature_id"],
                "story": each["scope_story"] or None,
                "asked_by": each["asked_by"],
                "text": each["text"],
                "why": each["why"],
            }
            for each in questions
        ],
        "waiting_for_the_product_owner": [item_facts(each) for each in waiting],
        "prepared_and_not_confirmed": [
            {
                "code": each["code"],
                "action": each["verb"],
                "of": each["target"],
                "text": each["said"],
                "reason": each["reason"],
                "why": each["why"],
                "prepared_by": each["prepared_by"],
                "prepared_at": each["prepared_at"].isoformat(),
                "expires_at": each["expires_at"].isoformat(),
            }
            for each in prepared
        ],
        "analysis": (
            None
            if analysis is None
            else {
                "version": analysis["version"],
                "feature": analysis["feature_id"],
                "text": analysis["text"],
                "written": WRITTEN_BY,
                "recorded_at": analysis["recorded_at"].isoformat(),
            }
        ),
        "notes": [
            {
                "note": str(each["note_id"]),
                "feature": each["feature_id"],
                "question": each["text"],
                "written": WRITTEN_BY,
                **(
                    {"outstanding": False, "answer_in_the_persons_words": each["answer"]}
                    if each["answer"] is not None
                    else {
                        "outstanding": True,
                        "said_by_the_person_since": list(each["said_since"]),
                    }
                ),
            }
            for each in notes
        ],
    }
