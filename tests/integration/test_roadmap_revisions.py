"""A feature's roadmap can be proposed again: after it was sent back, or to reword, move,
drop or add stories that have not started. A proposal is stored whole and touches nothing
until a person approves it; then it replaces the roadmap in force, all at once."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application import stories as stories_module
from relay.application.stories import Dropped, Kept, Reworded, StoryDraft
from relay.domain.story import ActionRefused, StoryState
from tests.integration.fixtures import ANALYST
from tests.integration.story_world import GREETS, STANDARD_RATE, StoryWorld, at_the_story_gate
from tests.integration.trunk_world import PROJECT

REFUNDS = StoryDraft("S3", "Refunds", "a refund is shown", {"AC1": "a refund of 120 shows 120"})
POLITER = Reworded(
    "S2", "A warmer greeting", "the shopper is greeted by name", {"AC1": "hello, Ada is printed"},
    story_revision=1, story_basis=0,
)  # fmt: skip


@pytest.fixture
async def world(database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> StoryWorld:
    world = StoryWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    return world


async def text_of(world: StoryWorld, story: str) -> tuple[Any, ...]:
    async with world.database.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT title, revision FROM stories WHERE project_id = %s AND story_id = %s",
                (PROJECT, story),
            )
        ).fetchone()
    assert row is not None
    return row["title"], row["revision"]


async def test_a_story_exists_once_its_roadmap_is_approved_and_not_before(
    world: StoryWorld,
) -> None:
    gate = await world.roadmap(STANDARD_RATE, GREETS)

    before = await world.stories.roadmap_of(PROJECT, "F1")
    assert (before.effective, before.proposed, before.stories) == (None, 1, ())
    with pytest.raises(LookupError):
        await world.story("S1")

    await world.decide(gate)

    after = await world.stories.roadmap_of(PROJECT, "F1")
    assert (after.effective, after.proposed, after.stories) == (1, None, ("S1", "S2"))
    assert (await world.story("S1"), await world.story("S2")) == (
        StoryState.RELEASED,
        StoryState.PLANNED,
    )


async def test_a_roadmap_that_was_sent_back_can_be_proposed_again(world: StoryWorld) -> None:
    first = await world.roadmap(STANDARD_RATE, GREETS)
    await world.decide(first, "request_changes", reason="one story is enough")
    assert (await world.stories.roadmap_of(PROJECT, "F1")).proposed is None

    again = await world.revise(STANDARD_RATE)

    assert again != first
    assert (await world.stories.roadmap_of(PROJECT, "F1")).proposed == 2
    await world.decide(again)
    assert await world.story("S1") is StoryState.RELEASED
    assert (await world.stories.roadmap_of(PROJECT, "F1")).stories == ("S1",)
    with pytest.raises(LookupError):
        await world.story("S2")


async def test_a_planned_story_is_reworded_only_when_the_revision_is_approved(
    world: StoryWorld,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))

    gate = await world.revise(Kept("S1"), POLITER)

    assert await text_of(world, "S2") == ("Greeting", 1)
    assert (await world.stories.roadmap_of(PROJECT, "F1")).effective == 1
    [item] = await world.stories.inbox(PROJECT)
    assert item.manifest["changes"] == {
        "kept": ["S1"],
        "reworded": [
            {
                "story_id": "S2",
                "from": {
                    "title": "Greeting",
                    "outcome": "the shopper is greeted",
                    "criteria": {"AC1": "hello is printed"},
                },
                "to": {
                    "title": "A warmer greeting",
                    "outcome": "the shopper is greeted by name",
                    "criteria": {"AC1": "hello, Ada is printed"},
                },
            }
        ],
        "dropped": [],
        "added": [],
    }

    await world.decide(gate)

    assert await text_of(world, "S2") == ("A warmer greeting", 2)
    assert await world.story("S2") is StoryState.PLANNED
    assert (await world.stories.roadmap_of(PROJECT, "F1")).effective == 2
    assert await world.column("SELECT state FROM roadmaps ORDER BY revision") == [
        "superseded",
        "approved",
    ]
    assert await world.column(
        "SELECT manifest->'stories'->0->>'story_id' FROM gates WHERE kind = 'roadmap' "
        "ORDER BY opened_at"
    ) == ["S1", "S1"]


async def test_a_dropped_story_is_withdrawn_and_an_added_one_takes_its_turn(
    world: StoryWorld,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))

    await world.decide(
        await world.revise(REFUNDS, Kept("S1"), Dropped("S2", story_revision=1, story_basis=0))
    )

    assert await world.story("S2") is StoryState.WITHDRAWN
    assert await world.story("S3") is StoryState.PLANNED
    with pytest.raises(ActionRefused, match="S2 is already the name of a story"):
        # A name that was used is used: a story that was dropped is not proposed afresh.
        await world.revise(Kept("S3"), Kept("S1"), GREETS)
    assert (await world.stories.roadmap_of(PROJECT, "F1")).stories == ("S3", "S1")
    with pytest.raises(ActionRefused, match="not being worked on"):
        await world.behaviours.propose(PROJECT, "S2", "B9", "too late", ["AC1"], basis=0)


async def test_what_is_under_way_or_finished_cannot_be_changed_by_a_revision(
    world: StoryWorld,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    reworded_s1 = Reworded("S1", "Another rate", "x", {"AC1": "y"}, story_revision=1, story_basis=0)

    for entries, why in (
        ([reworded_s1, Kept("S2")], "S1 is released"),
        ([Dropped("S1", story_revision=1, story_basis=0), Kept("S2")], "S1 is released"),
        ([Kept("S1")], "not accounted for"),
        ([Kept("S1"), Kept("S2"), GREETS], "appears twice"),
        ([Kept("S1"), Kept("S2"), Kept("S9")], "not a story of the roadmap"),
    ):
        with pytest.raises(ActionRefused, match=why):
            await world.revise(*entries)

    assert (await world.stories.roadmap_of(PROJECT, "F1")).proposed is None


async def test_a_proposal_names_the_roadmap_and_the_proposal_it_was_written_against(
    world: StoryWorld,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS, REFUNDS))
    reordered = [Kept("S1"), Kept("S3"), Kept("S2")]
    the_old_order = [Kept("S1"), Kept("S2"), Kept("S3")]
    await world.decide(await world.revise(*reordered))  # revision 2, by someone else

    with pytest.raises(ActionRefused, match="written against roadmap revision 1"):
        # Drafted against revision 1, delivered late: it would quietly restore the old order.
        await world.stories.publish_roadmap(
            ANALYST, PROJECT, "F1", the_old_order, basis=0, revises=1, replaces=None
        )

    pending = await world.revise(*the_old_order)
    with pytest.raises(ActionRefused, match="proposal 3 is waiting"):
        await world.stories.publish_roadmap(
            ANALYST, PROJECT, "F1", reordered, basis=0, revises=2, replaces=None
        )
    replacement = await world.stories.publish_roadmap(
        ANALYST, PROJECT, "F1", reordered, basis=0, revises=2, replaces=3
    )

    assert (await world.decide(pending)).disposition == "stale"
    assert await world.column("SELECT state FROM roadmaps ORDER BY revision") == [
        "superseded",
        "approved",
        "replaced",
        "proposed",
    ]
    await world.decide(replacement)
    assert (await world.stories.roadmap_of(PROJECT, "F1")).stories == ("S1", "S3", "S2")


async def test_an_approval_is_stale_if_what_the_proposal_changes_has_started_meanwhile(
    world: StoryWorld,
) -> None:
    story_gate, _ = await at_the_story_gate(world)
    proposal = await world.revise(Kept("S1"), POLITER)
    await world.decide(story_gate)  # S1 approved, so S2 is released as it stands
    assert await world.story("S2") is StoryState.RELEASED

    receipt = await world.decide(proposal)

    assert receipt.disposition == "stale"
    assert await text_of(world, "S2") == ("Greeting", 1)
    assert (await world.stories.roadmap_of(PROJECT, "F1")).effective == 1


async def test_a_revision_and_the_release_of_the_story_it_rewords_do_not_cross(
    world: StoryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proposal's approval has the project and is held there. Story 1's approval,
    which would release story 2 as it stands, waits; then releases it as reworded."""
    story_gate, _ = await at_the_story_gate(world)
    proposal = await world.revise(Kept("S1"), POLITER)
    holding, go_on = asyncio.Event(), asyncio.Event()
    real = stories_module._locked_project

    async def held_with_the_project(*arguments: Any) -> None:
        await real(*arguments)
        if not holding.is_set():
            holding.set()
            await go_on.wait()

    monkeypatch.setattr(stories_module, "_locked_project", held_with_the_project)
    revising = asyncio.create_task(world.decide(proposal))
    await holding.wait()
    approving = asyncio.create_task(world.decide(story_gate))
    await world.until_someone_waits_for_a_lock()
    go_on.set()

    assert (await revising).disposition == "effective"
    assert (await approving).disposition == "effective"
    assert await world.story("S2") is StoryState.RELEASED
    assert await text_of(world, "S2") == ("A warmer greeting", 2)
