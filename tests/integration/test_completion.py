"""Completing a feature. When its last story is accepted, Relay checks everything the
project's trunk must still satisfy, on the commit that is trunk then, and only if every
check passes records the feature as completed, with a summary taken from records."""

from __future__ import annotations

from pathlib import Path

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.domain.story import Receipt
from tests.integration.completion_world import CompletionWorld, last_story_at_its_gate
from tests.integration.trunk_world import PROJECT


@pytest.fixture
async def world(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> CompletionWorld:
    world = CompletionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    return world


async def test_accepting_the_last_story_completes_the_feature_with_nothing_else_called(
    world: CompletionWorld,
) -> None:
    gate = await last_story_at_its_gate(world)
    [obligation] = await world.column("SELECT obligation_id::text FROM obligations")

    assert await world.decide(gate) == Receipt("effective", "approved")

    assert await world.feature() == "completed"
    assert await world.batches() == ["completed"]
    summary = await world.summary()
    assert summary is not None
    assert (summary["feature_id"], summary["trunk"]) == ("F1", world.trunk())
    assert summary["usage"] == "unknown"
    [story] = summary["stories"]
    assert (story["story_id"], story["outcome"], story["gate_id"]) == ("S1", "accepted", str(gate))
    assert (
        story["manifest_digest"]
        == (await world.column(f"SELECT manifest_digest FROM gates WHERE gate_id = '{gate}'"))[0]
    )
    [checked] = summary["checked"]
    assert (checked["obligation_id"], checked["satisfied"]) == (obligation, True)
    assert await world.column(
        "SELECT revision FROM executions WHERE purpose = 'final_check' "
        f"AND execution_id = '{checked['execution_id']}'"
    ) == [world.trunk()]
    assert [each["behaviour_id"] for each in summary["behaviours"]] == ["B1"]
    assert (await world.events()).count("feature.completed") == 1
    assert await world.stories.inbox(PROJECT) == []


async def test_an_approval_sent_again_after_its_reply_was_lost_completes_the_feature_once(
    world: CompletionWorld,
) -> None:
    gate = await last_story_at_its_gate(world)
    approval = world.action(gate)

    first = await world.features.decide(approval)
    again = await world.features.decide(approval)

    assert first == again == Receipt("effective", "approved")
    assert await world.batches() == ["completed"]
    assert (await world.events()).count("feature.completed") == 1
    assert await world.column(
        "SELECT count(*) FROM outbox WHERE envelope->>'event_type' = 'feature.completed'"
    ) == [1]


async def test_a_feature_with_a_story_still_to_come_is_not_completed(
    world: CompletionWorld,
) -> None:
    from tests.integration.story_world import GREETS

    gate = await last_story_at_its_gate(world, GREETS)

    await world.decide(gate)

    assert await world.feature() == "delivering"
    assert await world.batches() == []
    assert await world.summary() is None
