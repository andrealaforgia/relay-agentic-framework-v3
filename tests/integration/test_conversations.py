"""The Product Owner's conversation as Relay records it (3C-2.9, 3C-3.6, 3C-3.7)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application import conversations, prepared
from relay.application.usage import account_text, live_usage
from tests.integration.decision_world import DecisionWorld
from tests.integration.story_world import STANDARD_RATE
from tests.integration.trunk_world import PROJECT

MODEL = "claude-haiku-5-5"
USED = [
    {
        "model": MODEL,
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "provider_cost_usd": "0.001",
        "estimated_cost_usd": "0.0009",
        "price_table": "2026-11-03",
    }
]


async def rows(world: DecisionWorld, query: str) -> list[dict[str, Any]]:
    async with world.database.connection() as connection:
        return [dict(each) for each in await (await connection.execute(query)).fetchall()]


@pytest.fixture
async def world(database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> DecisionWorld:
    world = DecisionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    return world


async def test_a_turn_is_on_record_before_its_call_and_is_settled_once(
    world: DecisionWorld,
) -> None:
    pool = world.database
    talk = await conversations.open_conversation(pool, PROJECT)

    first = await conversations.begin_turn(pool, talk, "  what is waiting?  ", MODEL)

    [row] = await rows(world, "SELECT * FROM conversation_turns")
    assert (row["number"], row["said"], row["state"]) == (1, "  what is waiting?  ", "intended")
    assert (row["role"], row["ended_at"], row["turn_id"]) == ("interpreter", None, first.turn_id)
    assert await conversations.provider_session_of(pool, talk) is None
    theirs = uuid.uuid4()
    assert await conversations.settle_turn(
        pool, first.turn_id, "applied", reply="one gate", provider_session=theirs, continued=False,
        usage=USED, provider_cost_usd=Decimal("0.001"), estimated_cost_usd=Decimal("0.0009"),
        price_table="2026-11-03", envelope={"subtype": "success"},
    )  # fmt: skip
    # Once: a second settling changes nothing of the first.
    assert not await conversations.settle_turn(pool, first.turn_id, "errored", reason="again")
    [row] = await rows(world, "SELECT * FROM conversation_turns")
    assert (row["state"], row["reply"], row["reason"]) == ("applied", "one gate", None)
    assert row["ended_at"] is not None
    assert await conversations.provider_session_of(pool, talk) == theirs
    second = await conversations.begin_turn(pool, talk, "and then?", MODEL)
    assert second.number == 2 and second.turn_id != first.turn_id  # a fresh identity each
    with pytest.raises(conversations.NotThisConversation):
        await conversations.begin_turn(pool, uuid.uuid4(), "to nobody", MODEL)


async def test_turns_are_accounted_like_any_call_and_a_lost_one_is_uncertain(
    world: DecisionWorld,
) -> None:
    """3C-3.6: two turns of one conversation; the terminal result of the second lost. The
    cost view shows the known cost and the uncertain call, under a role of their own."""
    pool = world.database
    talk = await conversations.open_conversation(pool, PROJECT)
    first = await conversations.begin_turn(pool, talk, "what is waiting?", MODEL)
    await conversations.settle_turn(
        pool, first.turn_id, "applied", reply="one gate", usage=USED,
        provider_cost_usd=Decimal("0.001"), estimated_cost_usd=Decimal("0.0009"),
        price_table="2026-11-03",
    )  # fmt: skip
    second = await conversations.begin_turn(pool, talk, "approve it", MODEL)
    in_flight = await live_usage(pool, PROJECT)
    assert in_flight["by_role"]["interpreter"]["calls_in_flight"] == 1
    await conversations.settle_turn(
        pool, second.turn_id, "uncertain", reason="the run ended without a result"
    )

    account = await live_usage(pool, PROJECT)

    interpreter = account["by_role"]["interpreter"]
    assert (interpreter["calls"], interpreter["calls_whose_end_is_not_known"]) == (1, 1)
    assert interpreter["charged_by_the_provider"]["usd"] == "0.001"
    assert interpreter["estimated_by_relay"] == [
        {"price_table": "2026-11-03", "usd": "0.0009", "calls": 1}
    ]
    assert (account["calls"], account["calls_whose_end_is_not_known"]) == (1, 1)
    assert account["by_model"][MODEL]["estimated_by_relay"][0]["usd"] == "0.0009"
    assert "the interpreter: 1 call(s), and 1 whose end is not known" in account_text(account)
    # For a project, and for no feature: a feature's account is of its own work.
    await world.decide(await world.roadmap(STANDARD_RATE))
    assert (await live_usage(pool, PROJECT, "F1"))["by_role"] == {}


async def test_a_turn_whose_host_died_is_said_to_be_uncertain_once_it_is_old(
    world: DecisionWorld,
) -> None:
    pool = world.database
    talk = await conversations.open_conversation(pool, PROJECT)
    await conversations.begin_turn(pool, talk, "hello", MODEL)

    assert await conversations.give_up_on_turns_left_open(pool, PROJECT, older_than=600) == 0
    assert await conversations.give_up_on_turns_left_open(pool, PROJECT, older_than=0) == 1

    [row] = await rows(world, "SELECT state, reason, ended_at FROM conversation_turns")
    assert row["state"] == "uncertain" and "before this turn was settled" in row["reason"]
    assert row["ended_at"] is not None


async def test_an_analysis_is_kept_in_versions_and_decides_nothing(world: DecisionWorld) -> None:
    pool = world.database
    talk = await conversations.open_conversation(pool, PROJECT)

    before_any_feature = await conversations.record_analysis(pool, talk, "  VAT is added twice. ")
    await world.decide(await world.roadmap(STANDARD_RATE))
    for_the_feature = await conversations.record_analysis(pool, talk, "Only on export.", "F1")

    assert (before_any_feature, for_the_feature) == (1, 2)
    assert await conversations.analysis_of(pool, PROJECT, 1) == {
        "version": 1,
        "feature_id": None,
        "text": "VAT is added twice.",
        "recorded_at": (await rows(world, "SELECT recorded_at FROM analyses ORDER BY version"))[0][
            "recorded_at"
        ],
    }
    latest = (await conversations.context(pool, PROJECT))["analysis"]
    assert (latest["version"], latest["feature"], latest["text"]) == (2, "F1", "Only on export.")
    assert latest["written"] == "written by a model, not by the Product Owner"
    with pytest.raises(conversations.NotThisConversation):
        await conversations.record_analysis(pool, talk, "of nothing", "F9")
    with pytest.raises(ValueError):
        await conversations.record_analysis(pool, talk, "   ")
    assert await conversations.analysis_of(pool, PROJECT, 9) is None
    # Nothing was decided or started by any of it.
    assert await world.column("SELECT count(*) FROM features") == [1]
    assert await world.column("SELECT count(*) FROM decisions") == [0]


async def test_the_persons_own_words_are_kept_against_the_note_they_answered(
    world: DecisionWorld,
) -> None:
    """3C-3.7. The model names the note; the words are the person's, as typed."""
    pool = world.database
    talk = await conversations.open_conversation(pool, PROJECT)
    with pytest.raises(conversations.NotThisConversation):
        await conversations.note_answered(pool, talk, "0")  # nothing has been said yet
    note = await conversations.note_question(pool, talk, " Does it apply to exports? ")
    other = await conversations.note_question(pool, talk, "And to books?")
    await conversations.begin_turn(pool, talk, "No, exports are zero-rated.", MODEL)

    # The session is closed here, before any model replied (the acceptance history): a
    # new session's context has the note and what the person typed after it.
    await conversations.close_conversation(pool, talk)
    [open_note, _] = (await conversations.context(pool, PROJECT))["notes"]
    assert open_note == {
        "note": str(note),
        "feature": None,
        "question": "Does it apply to exports?",
        "written": conversations.WRITTEN_BY,
        "outstanding": True,
        "said_by_the_person_since": ["No, exports are zero-rated."],
    }

    words = await conversations.note_answered(pool, talk, str(note)[:23])

    assert words == "No, exports are zero-rated."
    [answered, still_open] = (await conversations.context(pool, PROJECT))["notes"]
    assert answered["outstanding"] is False
    assert answered["answer_in_the_persons_words"] == "No, exports are zero-rated."
    assert "said_by_the_person_since" not in answered
    assert still_open["note"] == str(other) and still_open["outstanding"] is True
    with pytest.raises(conversations.NotThisConversation, match="has its answer already"):
        await conversations.note_answered(pool, talk, str(note))
    with pytest.raises(conversations.NotThisConversation, match="no one note"):
        await conversations.note_answered(pool, talk, str(note)[:8])  # two begin alike
    with pytest.raises(conversations.NotThisConversation, match="no one note"):
        await conversations.note_answered(pool, talk, "ffffffff")
    with pytest.raises(ValueError):
        await conversations.note_question(pool, talk, "")
    with pytest.raises(conversations.NotThisConversation):
        await conversations.note_question(pool, talk, "of nothing?", "F9")


async def test_context_is_what_a_new_session_needs_from_the_record(world: DecisionWorld) -> None:
    """3C-1.11: the feature, the decisions in force with who made them, open questions,
    what is waiting, and what was prepared and not confirmed, with who prepared it and why."""
    pool = world.database
    gate = await world.roadmap(STANDARD_RATE)
    code = (
        await prepared.prepare(
            pool, PROJECT, "approve", str(gate), None, None, "the roadmap is what was asked",
            "shown", "basis", by="the Interpreter",
        )
    ).code  # fmt: skip
    gone = await prepared.prepare(
        pool, PROJECT, "approve", str(gate), None, None, "an hour ago", "shown", "basis",
        good_for=timedelta(seconds=-1),
    )  # fmt: skip
    ended = await prepared.prepare(pool, PROJECT, "approve", str(gate), None, None, "x", "s", "b")
    await prepared.end(pool, ended.code, "stale")

    told = await conversations.context(pool, PROJECT)

    assert told["project"] == PROJECT
    [feature] = told["features"]
    assert (feature["feature"], feature["state"]) == ("F1", "planning") and feature["problem"]
    [waiting] = told["waiting_for_the_product_owner"]
    assert waiting["kind"] == "gate" and str(gate).startswith(waiting["handle"])
    [action] = told["prepared_and_not_confirmed"]  # not the expired one, nor the ended one
    assert (action["code"], action["action"], action["of"]) == (code, "approve", str(gate))
    assert (action["why"], action["prepared_by"]) == (
        "the roadmap is what was asked",
        "the Interpreter",
    )
    assert gone.code != code and told["analysis"] is None and told["notes"] == []
    assert told["decisions_in_force"] == [] and told["open_questions"] == []

    await world.decide(gate)
    question = await world.ask()
    [asked] = (await conversations.context(pool, PROJECT))["open_questions"]
    assert asked["question"] == str(question) and asked["feature"] == "F1" and asked["text"]
    await world.answer(question, "zero-rated")
    after = await conversations.context(pool, PROJECT)
    assert after["open_questions"] == [] and after["waiting_for_the_product_owner"] == []
    [decision] = after["decisions_in_force"]
    assert (decision["decision"], decision["made_by"], decision["text"]) == (
        "D1",
        "the Product Owner",
        "zero-rated",
    )
