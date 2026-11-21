"""Rejecting a story, and correcting it. A rejection is for good: what the story put on
trunk stays, and the way forward is another story that corrects it, proposed in a roadmap
revision and approved by a person. Until that correction is approved, nothing ordinary in
the feature starts."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.behaviours import Behaviours
from relay.application.stories import Dropped, Kept, StoryDraft
from relay.domain.behaviour import BehaviourState
from relay.domain.evidence import RedRejection
from relay.domain.story import ActionRefused, HumanAction, Receipt, StoryState
from relay.testing.oracle import ContainerIsolation, ProcessOracle
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import (
    ANALYST,
    CONTRACT,
    PRODUCT_OWNER,
    HeldAtABarrier,
    held_at_the_barrier,
    run_green,
)
from tests.integration.story_world import GREETS, STANDARD_RATE, at_the_story_gate
from tests.integration.trunk_world import GREETING, PROJECT

CORRECTION = StoryDraft(
    "C1", "Standard rate, shown properly", "the gross price is shown with its currency",
    {"AC1": "100 net is shown as 120"}, corrects="S1",
)  # fmt: skip
ANOTHER = StoryDraft("T1", "Another feature", "something else", {"AC1": "it works"})


@pytest.fixture
async def world(database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> DecisionWorld:
    world = DecisionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    return world


async def reject(world: DecisionWorld, story: str, **named: Any) -> Receipt:
    """What the Product Owner does to set aside a story that is being worked on: it
    names the revision and the decisions it was looking at."""
    revision, _ = await world.stories.revisions_of(PROJECT, story)
    world.requests += 1
    payload = {
        "reason": "this is not what was wanted",
        "revision": str(named.get("revision", revision)),
        "basis": str(named.get("basis", await world.basis(story))),
    }
    return await world.stories.reject(
        PROJECT, HumanAction(PRODUCT_OWNER, f"reject-{world.requests}", story, "reject", payload)
    )


async def rejected_at_its_gate(world: DecisionWorld) -> str:
    """Story 1 of two, delivered and then rejected at its gate. Returns its commit."""
    gate, candidate = await at_the_story_gate(world)
    assert await world.decide(gate, "reject", reason="the currency is missing") == Receipt(
        "effective", "rejected"
    )
    return candidate


async def corrections_owed(world: DecisionWorld) -> list[object]:
    return await world.column(
        "SELECT state FROM assignments WHERE kind = 'correct' ORDER BY created_at"
    )


async def test_a_rejected_story_stays_on_trunk_and_stops_ordinary_work_in_its_feature(
    world: DecisionWorld,
) -> None:
    await world.stories.open_feature(PRODUCT_OWNER, PROJECT, "F2", "something else")
    candidate = await rejected_at_its_gate(world)

    assert (await world.story("S1"), await world.story("S2")) == (
        StoryState.REJECTED,
        StoryState.PLANNED,
    )
    assert world.trunk() == candidate
    assert await world.state("B1") is BehaviourState.INTEGRATED
    assert await world.stories.inbox(PROJECT) == []
    assert await corrections_owed(world) == ["wanted"]
    assert (await world.events())[-1] == "gate.rejected"

    other = await world.stories.publish_roadmap(ANALYST, PROJECT, "F2", [ANOTHER], basis=0)
    await world.decide(other)
    assert await world.story("T1") is StoryState.RELEASED  # a rejection does not hold the place
    assert await world.story("S2") is StoryState.PLANNED


async def test_a_roadmap_gate_is_not_a_place_to_reject_a_story(world: DecisionWorld) -> None:
    gate = await world.roadmap(STANDARD_RATE)

    with pytest.raises(ActionRefused, match="not something that can be done"):
        await world.decide(gate, "reject", reason="no")
    story_gate, _ = await at_the_story_gate_after(world, gate)
    with pytest.raises(ActionRefused, match="reason"):
        await world.decide(story_gate, "reject", reason=" ")
    assert await world.story("S1") is StoryState.AWAITING_APPROVAL


async def at_the_story_gate_after(
    world: DecisionWorld, roadmap_gate: uuid.UUID
) -> tuple[uuid.UUID, str]:
    await world.decide(roadmap_gate)
    candidate = await world.deliver("B1")
    await world.seal()
    await world.assess("B1 shows it")
    return await world.gate(), candidate


@pytest.mark.parametrize("order", [["S1", "S2", "C1"], ["S2", "S1", "C1"], ["C1", "S1", "S2"]])
async def test_the_correction_starts_and_the_ordinary_story_waits_wherever_they_are_put(
    world: DecisionWorld, order: list[str]
) -> None:
    await rejected_at_its_gate(world)
    with pytest.raises(ActionRefused, match="S1 is rejected and nothing in this roadmap corrects"):
        await world.revise(Kept("S1"), Kept("S2"))
    entries = [CORRECTION if story == "C1" else Kept(story) for story in order]

    await world.decide(await world.revise(*entries))

    assert (await world.story("C1"), await world.story("S2")) == (
        StoryState.RELEASED,
        StoryState.PLANNED,
    )
    assert await corrections_owed(world) == ["done"]
    await world.features.start()  # as after a restart: nothing more is released
    assert await world.story("S2") is StoryState.PLANNED


async def test_ordinary_work_resumes_when_a_chain_of_corrections_ends_in_an_approval(
    world: DecisionWorld,
) -> None:
    await rejected_at_its_gate(world)
    await world.decide(await world.revise(Kept("S1"), Kept("S2"), CORRECTION))
    assert (await reject(world, "C1")).disposition == "effective"
    assert await corrections_owed(world) == ["done", "wanted"]
    again = StoryDraft(
        "D1", "A greeting, since the rate is settled", "the shopper is greeted",
        {"AC1": "hello is printed"}, corrects="C1",
    )  # fmt: skip
    with pytest.raises(ActionRefused, match="C1 is rejected and nothing in this roadmap corrects"):
        await world.revise(Kept("S1"), Kept("S2"), Kept("C1"))
    with pytest.raises(ActionRefused, match="S1 would have two corrections"):
        await world.revise(
            Kept("S1"), Kept("S2"), Kept("C1"), again,
            StoryDraft("E1", "x", "y", {"AC1": "z"}, corrects="S1"),
        )  # fmt: skip

    await world.decide(await world.revise(Kept("S1"), Kept("S2"), Kept("C1"), again))

    assert (await world.story("D1"), await world.story("S2")) == (
        StoryState.RELEASED,
        StoryState.PLANNED,
    )
    await world.deliver("B2", "D1", GREETING, {"greeting.txt": "hello\n"})
    await world.seal("D1")
    await world.assess("B2 greets", "D1")
    await world.decide(await world.gate())
    assert await world.story("D1") is StoryState.APPROVED
    assert await world.story("S2") is StoryState.RELEASED
    with pytest.raises(ActionRefused, match="C1 would have two corrections"):
        # Answered already: a rejection never has two corrections.
        await world.revise(
            Kept("S1"), Kept("S2"), Kept("C1"), Kept("D1"),
            StoryDraft("E1", "x", "y", {"AC1": "z"}, corrects="C1"),
        )  # fmt: skip


async def test_a_story_being_worked_on_is_set_aside_as_the_person_saw_it(
    world: DecisionWorld,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.deliver("B1")
    base = await world.awaiting_builder("B2", GREETING)
    world.publish_attempt("B2", {"greeting.txt": "hello\n"}, on=base)

    with pytest.raises(ActionRefused, match="human"):
        await world.stories.reject(
            PROJECT,
            HumanAction(
                ANALYST, "x", "S1", "reject", {"reason": "r", "revision": "1", "basis": "0"}
            ),
        )
    with pytest.raises(ActionRefused, match="only that"):
        await world.stories.reject(
            PROJECT, HumanAction(PRODUCT_OWNER, "x", "S1", "reject", {"reason": "r"})
        )
    assert (await reject(world, "S1", revision=7)).disposition == "stale"
    assert (await reject(world, "S1", basis=3)).disposition == "stale"
    assert await world.story("S1") is StoryState.RELEASED

    receipt = await reject(world, "S1")

    assert receipt == Receipt("effective", "rejected")
    assert await world.story("S1") is StoryState.REJECTED
    assert await world.state("B1") is BehaviourState.INTEGRATED
    assert await world.column(
        "SELECT kind || ':' || state FROM assignments WHERE behaviour_id = 'B2'"
    ) == ["build:retired"]
    with pytest.raises(ActionRefused, match="not being worked on"):
        await run_green(world.behaviours, PROJECT, "B2", 1)
    assert (await reject(world, "S1")).disposition == "stale"  # there is nothing left to reject
    assert (await reject(world, "S2")).disposition == "stale"  # a planned story has not started


async def test_a_rejection_prepared_for_one_revision_does_not_land_on_the_next(
    world: DecisionWorld,
) -> None:
    gate, _ = await at_the_story_gate(world)
    prepared = HumanAction(
        PRODUCT_OWNER, "late", "S1", "reject", {"reason": "no", "revision": "1", "basis": "0"}
    )
    await world.decide(gate, "request_changes", reason="show the currency too")
    assert await world.stories.revisions_of(PROJECT, "S1") == (2, 1)

    first = await world.stories.reject(PROJECT, prepared)

    assert first.disposition == "stale"
    assert await world.story("S1") is StoryState.CHANGES_REQUESTED
    assert await world.stories.reject(PROJECT, prepared) == first
    assert await corrections_owed(world) == []


async def test_a_correction_that_is_stopped_does_not_start_and_nothing_ordinary_does_either(
    world: DecisionWorld,
) -> None:
    await world.stories.open_feature(PRODUCT_OWNER, PROJECT, "F2", "something else")
    await rejected_at_its_gate(world)
    await world.decide(
        await world.stories.publish_roadmap(ANALYST, PROJECT, "F2", [ANOTHER], basis=0)
    )
    assert await world.story("T1") is StoryState.RELEASED  # the other feature has the place
    await world.decide(await world.revise(Kept("S1"), Kept("S2"), CORRECTION))
    with pytest.raises(ActionRefused, match="S1 is rejected and nothing in this roadmap corrects"):
        # The only correction of a rejection cannot be dropped.
        await world.revise(Kept("S1"), Kept("S2"), Dropped("C1", story_revision=1, story_basis=0))
    question = await world.ask("C1")

    await world.close("cancel", feature="F2", reason="dropped")  # the place falls free

    assert (await world.story("C1"), await world.story("S2")) == (
        StoryState.PLANNED,
        StoryState.PLANNED,
    )
    await world.answer(question)
    await world.reconfirm_everything()
    assert (await world.story("C1"), await world.story("S2")) == (
        StoryState.RELEASED,
        StoryState.PLANNED,
    )
    assert (await world.events()).count("story.released") == 3


async def test_a_story_that_is_rejected_or_withdrawn_is_history_to_questions_and_decisions(
    world: DecisionWorld,
) -> None:
    await rejected_at_its_gate(world)
    await world.decide(
        await world.revise(Kept("S1"), CORRECTION, Dropped("S2", story_revision=1, story_basis=0))
    )

    for story in ("S1", "S2"):
        with pytest.raises(ActionRefused, match="history"):
            await world.ask(story)
    await world.answer(await world.ask(story=None))

    assert sorted((each["subject"], each["story_id"]) for each in await world.owed()) == [
        ("roadmap", None),
        ("story", "C1"),
    ]


async def test_setting_aside_the_story_that_holds_the_place_lets_another_feature_start(
    world: DecisionWorld,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.decide(await world.roadmap(ANOTHER, feature="F2"))
    assert await world.story("T1") is StoryState.PLANNED

    await reject(world, "S1")

    assert await world.story("T1") is StoryState.RELEASED
    assert (await world.events())[-2:] == ["story.rejected", "story.released"]


async def test_a_red_that_finishes_after_its_story_was_set_aside_is_evidence_only(
    world: DecisionWorld,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.behaviours.propose(
        PROJECT, "S1", "B1", "standard rate", ["AC1"], basis=0, story_revision=1
    )
    await world.behaviours.begin_test_design(PROJECT, "B1")
    await world.behaviours.register_acceptance_test(PROJECT, "B1", CONTRACT, basis=0)
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    slow = Behaviours(world.database, world.mirrors, ProcessOracle(barrier))
    running = asyncio.create_task(slow.run_red(PROJECT, "B1"))
    await held_at_the_barrier(running, barrier)

    await reject(world, "S1")
    barrier.release.set()

    assert (await running).rejection is RedRejection.SUPERSEDED
    assert await world.state("B1") is BehaviourState.VALIDATING_RED
    assert await world.column("SELECT count(*) FROM assignments WHERE kind = 'build'") == [0]
    assert await world.column("SELECT count(*) FROM executions WHERE purpose = 'red'") == [1]
