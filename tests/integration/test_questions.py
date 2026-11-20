"""Questions for the Product Owner and the decisions that answer them. An open question
stops its scope, by being looked up wherever work starts; the answer is a decision of the
Product Owner's, kept apart from anything a model assumed, and shown as context."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application import behaviours as behaviours_module
from relay.application.assignments import Assignments
from relay.application.decisions import Decisions
from relay.application.integration import Outcome
from relay.application.stories import StoryDraft
from relay.domain.story import ActionRefused, HumanAction, Receipt, StoryState
from relay.testing.oracle import ContainerIsolation
from tests.integration.fixtures import (
    ANALYST,
    BUILDER,
    CONTRACT,
    PRODUCT_OWNER,
    VERIFIER,
    HeldAtABarrier,
    held_at_the_barrier,
)
from tests.integration.story_world import GREETS, STANDARD_RATE, StoryWorld, at_the_story_gate
from tests.integration.trunk_world import PROJECT, ZERO_STAYS_ZERO

RATE = {
    "text": "Which VAT rate applies to books?",
    "why": "the expected gross amount depends on it",
    "options": ["zero-rated", "standard rate"],
}


class Asking(StoryWorld):
    def __init__(self, *arguments: Any, **options: Any) -> None:
        super().__init__(*arguments, **options)
        self.decisions = Decisions(self.database, self.features)
        self.asked = 0

    async def ask(self, story: str | None = "S1", feature: str = "F1") -> uuid.UUID:
        self.asked += 1
        return await self.decisions.ask(
            VERIFIER, f"ask-{self.asked}", PROJECT, feature, story_id=story, **RATE
        )

    async def answer(self, question: uuid.UUID, text: str = "zero-rated") -> Receipt:
        self.requests += 1
        return await self.decisions.answer(
            HumanAction(
                PRODUCT_OWNER, f"answer-{self.requests}", str(question), "answer", {"text": text}
            )
        )

    async def replace(self, decision: uuid.UUID, text: str) -> Receipt:
        self.requests += 1
        return await self.decisions.replace(
            HumanAction(
                PRODUCT_OWNER, f"replace-{self.requests}", str(decision), "replace", {"text": text}
            )
        )


@pytest.fixture
async def world(database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> Asking:
    world = Asking(database, tmp_path, runner_image)
    world.base = await world.open()
    return world


async def test_an_open_question_stops_its_story_and_the_answer_becomes_shared_context(
    world: Asking,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"])
    await world.behaviours.begin_test_design(PROJECT, "B1")
    await world.behaviours.register_acceptance_test(PROJECT, "B1", CONTRACT)

    question = await world.ask("S1")

    with pytest.raises(ActionRefused, match=f"question {question}"):
        await world.behaviours.run_red(PROJECT, "B1")
    [waiting] = await world.decisions.open_questions(PROJECT)
    assert (waiting.question_id, waiting.text, waiting.why, waiting.options) == (
        question,
        RATE["text"],
        RATE["why"],
        ("zero-rated", "standard rate"),
    )
    assert (await world.decisions.context_for(PROJECT, "F1", "S1")).decisions == ()

    assert await world.answer(question) == Receipt("effective", "answered")

    assert await world.decisions.open_questions(PROJECT) == []
    context = await world.decisions.context_for(PROJECT, "F1", "S1")
    assert [(each.source, each.text) for each in context.decisions] == [
        ("product_owner", "zero-rated")
    ]
    assert context.basis == 1
    assert (await world.decisions.context_for(PROJECT, "F1", "S2")).basis == 0
    assert (await world.behaviours.run_red(PROJECT, "B1")).is_valid
    assert (await world.events()).count("question.asked") == 1
    assert "question.answered" in await world.events()


async def test_asking_again_after_a_lost_reply_is_the_same_question(world: Asking) -> None:
    await world.roadmap(STANDARD_RATE)

    first = await world.decisions.ask(VERIFIER, "ask-1", PROJECT, "F1", story_id="S1", **RATE)
    again = await world.decisions.ask(VERIFIER, "ask-1", PROJECT, "F1", story_id="S1", **RATE)

    assert again == first
    assert len(await world.decisions.open_questions(PROJECT)) == 1
    with pytest.raises(ActionRefused, match="already used"):
        await world.decisions.ask(
            VERIFIER, "ask-1", PROJECT, "F1", story_id="S1", **{**RATE, "text": "Another?"}
        )
    with pytest.raises(ActionRefused, match="worker"):
        await world.decisions.ask(PRODUCT_OWNER, "ask-2", PROJECT, "F1", **RATE)
    with pytest.raises(ActionRefused, match="needs"):
        await world.decisions.ask(VERIFIER, "ask-3", PROJECT, "F1", **{**RATE, "why": " "})


async def test_a_question_asked_before_any_roadmap_stops_planning_until_it_is_answered(
    world: Asking,
) -> None:
    await world.stories.open_feature(PRODUCT_OWNER, PROJECT, "F1", "prices need VAT")
    question = await world.ask(story=None)

    with pytest.raises(ActionRefused, match="question"):
        await world.stories.publish_roadmap(ANALYST, PROJECT, "F1", [STANDARD_RATE])

    await world.answer(question)
    await world.stories.publish_roadmap(ANALYST, PROJECT, "F1", [STANDARD_RATE])
    context = await world.decisions.context_for(PROJECT, "F1", "S1")
    assert [each.text for each in context.decisions] == ["zero-rated"]
    assert (await world.decisions.context_for(PROJECT, "F1")).basis == 1


async def test_only_a_person_answers_and_a_question_is_answered_once(world: Asking) -> None:
    await world.roadmap(STANDARD_RATE)
    question = await world.ask("S1")
    answer = HumanAction(PRODUCT_OWNER, "a1", str(question), "answer", {"text": "zero-rated"})

    with pytest.raises(ActionRefused, match="human"):
        await world.decisions.answer(
            HumanAction(VERIFIER, "x", str(question), "answer", {"text": "standard"})
        )
    with pytest.raises(ActionRefused, match="only that"):
        await world.decisions.answer(HumanAction(PRODUCT_OWNER, "x", str(question), "answer", {}))
    first = await world.decisions.answer(answer)

    assert first == await world.decisions.answer(answer) == Receipt("effective", "answered")
    another = await world.answer(question, "standard rate")
    assert another.disposition == "stale"
    assert [
        each.text for each in (await world.decisions.context_for(PROJECT, "F1", "S1")).decisions
    ] == ["zero-rated"]
    assert (await world.events()).count("question.answered") == 1


async def test_a_decision_is_replaced_once_and_keeps_its_scope(world: Asking) -> None:
    await world.roadmap(STANDARD_RATE, GREETS)
    await world.answer(await world.ask("S1"))
    [original] = (await world.decisions.context_for(PROJECT, "F1", "S1")).decisions

    assert await world.replace(original.decision_id, "standard rate after all") == Receipt(
        "effective", "replaced"
    )

    context = await world.decisions.context_for(PROJECT, "F1", "S1")
    assert [(each.text, each.replaces) for each in context.decisions] == [
        ("standard rate after all", original.decision_id)
    ]
    assert context.basis == 2
    assert (await world.decisions.context_for(PROJECT, "F1", "S2")).basis == 0
    assert (await world.replace(original.decision_id, "a third view")).disposition == "stale"
    with pytest.raises(ActionRefused, match="human"):
        await world.decisions.replace(
            HumanAction(ANALYST, "x", str(original.decision_id), "replace", {"text": "mine"})
        )


async def test_what_a_model_assumed_is_kept_apart_and_moves_nothing(world: Asking) -> None:
    await world.roadmap(STANDARD_RATE)

    assumed = await world.decisions.assume(
        ANALYST, PROJECT, "F1", story_id="S1", text="amounts are whole pounds"
    )

    context = await world.decisions.context_for(PROJECT, "F1", "S1")
    assert [(each.source, each.text) for each in context.decisions] == [
        ("model", "amounts are whole pounds")
    ]
    assert context.basis == 0
    await world.replace(assumed, "amounts are in pence")
    context = await world.decisions.context_for(PROJECT, "F1", "S1")
    assert [(each.source, each.text) for each in context.decisions] == [
        ("product_owner", "amounts are in pence")
    ]
    assert context.basis == 2
    with pytest.raises(ActionRefused, match="worker"):
        await world.decisions.assume(PRODUCT_OWNER, PROJECT, "F1", text="x")


async def test_a_stopped_story_is_not_released_and_nothing_in_its_feature_jumps_it(
    world: Asking,
) -> None:
    first = await world.roadmap(STANDARD_RATE, GREETS)
    question = await world.ask("S1")
    another = StoryDraft("T1", "Another feature", "something else", {"AC1": "it works"})
    second = await world.roadmap(another, feature="F2")

    await world.decide(first)
    assert (await world.story("S1"), await world.story("S2")) == (
        StoryState.PLANNED,
        StoryState.PLANNED,
    )
    await world.decide(second)
    assert await world.story("T1") is StoryState.RELEASED

    await world.answer(question)
    assert await world.story("S1") is StoryState.PLANNED  # the place is taken, for now


async def test_answering_releases_the_story_that_was_waiting_only_for_the_answer(
    world: Asking,
) -> None:
    gate = await world.roadmap(STANDARD_RATE, GREETS)
    question = await world.ask("S1")
    await world.decide(gate)
    assert await world.story("S1") is StoryState.PLANNED

    await world.answer(question)

    assert await world.story("S1") is StoryState.RELEASED
    assert (await world.events())[-2:] == ["question.answered", "story.released"]


async def test_a_gate_is_not_decided_while_a_question_covers_it(world: Asking) -> None:
    gate = await world.roadmap(STANDARD_RATE)
    question = await world.ask(story=None)

    with pytest.raises(ActionRefused, match="question"):
        await world.decide(gate)

    await world.answer(question)
    assert (await world.decide(gate)).disposition == "effective"


async def test_a_pause_and_a_question_each_stop_the_story_until_each_is_cleared(
    world: Asking,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"])
    feature_wide = await world.ask(story=None)
    about_the_story = await world.ask("S1")
    await world.close("pause", reason="budget review")

    await world.answer(about_the_story)
    with pytest.raises(ActionRefused, match="paused"):
        await world.behaviours.begin_test_design(PROJECT, "B1")
    await world.close("resume")
    with pytest.raises(ActionRefused, match=f"question {feature_wide}"):
        await world.behaviours.begin_test_design(PROJECT, "B1")
    await world.answer(feature_wide)

    await world.behaviours.begin_test_design(PROJECT, "B1")


async def test_cancelling_a_feature_withdraws_its_questions(world: Asking) -> None:
    await world.roadmap(STANDARD_RATE)
    question = await world.ask("S1")

    await world.close("cancel", reason="dropped")

    assert await world.decisions.open_questions(PROJECT) == []
    assert (await world.answer(question)).disposition == "stale"
    assert (await world.decisions.context_for(PROJECT, "F1", "S1")).decisions == ()
    with pytest.raises(ActionRefused, match="cancelled"):
        await world.ask("S1")


async def test_asking_takes_back_a_grant_that_has_done_nothing_yet(world: Asking) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.awaiting_builder("B1")
    [build] = await world.column("SELECT assignment_id FROM assignments")
    assert isinstance(build, uuid.UUID)
    claims = Assignments(world.database)
    before = await claims.claim(BUILDER, build)

    question = await world.ask("S1")

    assert await world.column("SELECT state FROM assignments") == ["wanted"]
    with pytest.raises(ActionRefused, match="question"):
        await claims.claim(BUILDER, build)
    await world.answer(question)
    assert await claims.claim(BUILDER, build) != before


async def test_checks_that_finish_under_a_question_run_again_once_it_is_answered(
    world: Asking,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.integration.require_check(PROJECT, ZERO_STAYS_ZERO)
    base = await world.awaiting_builder("B1")
    candidate = await world.reviewed("B1", on=base)
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    running = asyncio.create_task(world.integration_with(barrier).integrate(PROJECT, "B1", "once"))
    await held_at_the_barrier(running, barrier)
    question = await world.ask("S1")
    barrier.release.set()
    assert (await running).outcome is Outcome.HELD
    assert world.trunk() == base

    await world.answer(question)  # nothing else is called

    assert world.trunk() == candidate
    assert await world.column("SELECT count(*) FROM integration_intents") == [1]


async def test_a_question_waits_for_work_that_was_admitted_before_it(
    world: Asking, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"])
    admitted, go_on = asyncio.Event(), asyncio.Event()
    real = behaviours_module.require_story_at_work

    async def held_once_admitted(*arguments: Any, **options: Any) -> None:
        await real(*arguments, **options)
        admitted.set()
        await go_on.wait()

    monkeypatch.setattr(behaviours_module, "require_story_at_work", held_once_admitted)
    starting = asyncio.create_task(world.behaviours.begin_test_design(PROJECT, "B1"))
    await admitted.wait()
    asking = asyncio.create_task(world.ask("S1"))
    await world.until_someone_waits_for_a_lock()
    go_on.set()
    await starting
    await asking

    assert (await world.events())[-2:] == ["behaviour.test_design_started", "question.asked"]


async def test_a_question_is_about_a_feature_or_one_of_its_own_stories(world: Asking) -> None:
    await world.roadmap(STANDARD_RATE)
    another = StoryDraft("T1", "Another feature", "something else", {"AC1": "it works"})
    await world.roadmap(another, feature="F2")

    for feature, story, why_not in (
        ("F1", "S9", "not a story of"),
        ("F1", "T1", "not a story of"),
        ("F9", None, "unknown feature"),
    ):
        with pytest.raises(ActionRefused, match=why_not):
            await world.ask(story, feature=feature)

    assert await world.decisions.open_questions(PROJECT) == []


async def test_an_approved_story_is_beyond_questions(world: Asking) -> None:
    gate, _ = await at_the_story_gate(world)
    await world.decide(gate)
    assert await world.story("S1") is StoryState.APPROVED

    with pytest.raises(ActionRefused, match="correction"):
        await world.ask("S1")
    with pytest.raises(ActionRefused, match="correction"):
        await world.decisions.assume(ANALYST, PROJECT, "F1", story_id="S1", text="too late")

    await world.ask("S2")
