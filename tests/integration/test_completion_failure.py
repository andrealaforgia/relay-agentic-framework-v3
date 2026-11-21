"""A final check that fails on trunk. The feature is not completed; a person decides
whether the checks are run again or the roadmap is changed to put it right."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.stories import Kept
from relay.domain.story import ActionRefused, Receipt, StoryState
from tests.integration.completion_world import CompletionWorld, last_story_at_its_gate
from tests.integration.story_world import GREETS
from tests.integration.trunk_world import GREETING, PROJECT


@pytest.fixture
async def world(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> CompletionWorld:
    world = CompletionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    return world


async def failed_on_trunk(world: CompletionWorld) -> Any:
    """Every story accepted, and a check required since the last integration that trunk
    does not satisfy. Returns the inbox item of the gate that opened."""
    gate = await last_story_at_its_gate(world)
    await world.integration.require_check(PROJECT, GREETING.as_check())
    await world.decide(gate)
    [item] = await world.stories.inbox(PROJECT)
    return item


async def revisions_owed(world: CompletionWorld) -> list[object]:
    return await world.column(
        "SELECT role FROM assignments WHERE kind = 'revise' AND state IN ('wanted', 'granted')"
    )


async def test_a_check_that_fails_on_trunk_leaves_the_feature_for_a_person_to_decide(
    world: CompletionWorld,
) -> None:
    item = await failed_on_trunk(world)

    assert await world.feature() == "needs_decision"
    assert await world.batches() == ["failed"]
    assert await world.summary() is None
    assert item.kind == "completion"
    assert item.manifest["trunk"] == world.trunk()
    assert [(each["name"], each["satisfied"]) for each in item.manifest["checked"]] == [
        ("B1-standard-rate", True),
        (GREETING.contract_id, False),
    ]
    assert (await world.events()).count("feature.completed") == 0


async def test_only_a_retry_or_a_request_for_changes_is_decided_there(
    world: CompletionWorld,
) -> None:
    item = await failed_on_trunk(world)

    with pytest.raises(ActionRefused, match="retry"):
        await world.decide(item.gate_id)
    with pytest.raises(ActionRefused, match="retry"):
        await world.decide(item.gate_id, "reject", reason="no")

    assert await world.feature() == "needs_decision"
    assert [each.gate_id for each in await world.stories.inbox(PROJECT)] == [item.gate_id]


async def test_a_retry_is_refused_at_any_other_gate(world: CompletionWorld) -> None:
    gate = await last_story_at_its_gate(world)

    with pytest.raises(ActionRefused, match="final check"):
        await world.decide(gate, "retry")

    assert await world.story("S1") is StoryState.AWAITING_APPROVAL


async def test_a_retry_while_the_check_still_fails_asks_again_on_a_new_batch(
    world: CompletionWorld,
) -> None:
    item = await failed_on_trunk(world)

    assert await world.decide(item.gate_id, "retry") == Receipt("effective", "retried")

    assert await world.batches() == ["failed", "failed"]
    assert await world.feature() == "needs_decision"
    [again] = await world.stories.inbox(PROJECT)
    assert again.gate_id != item.gate_id
    assert (await world.decide(item.gate_id, "retry")).disposition == "stale"


async def test_a_retry_after_trunk_was_put_right_completes_the_feature_on_the_new_head(
    world: CompletionWorld,
) -> None:
    item = await failed_on_trunk(world)
    put_right = world.advance_trunk({"greeting.txt": "hello\n"})

    await world.decide(item.gate_id, "retry")

    assert await world.feature() == "completed"
    summary = await world.summary()
    assert summary is not None and summary["trunk"] == put_right
    assert all(each["satisfied"] for each in summary["checked"])


async def test_a_request_for_changes_keeps_the_feature_from_being_checked_until_its_roadmap_changes(
    world: CompletionWorld,
) -> None:
    item = await failed_on_trunk(world)

    assert await world.decide(item.gate_id, "request_changes", reason="greet the shopper") == (
        Receipt("effective", "changes_requested")
    )

    assert await world.feature() == "delivering"
    assert await revisions_owed(world) == ["analyst"]
    await world.features.start()  # a restart finds everything else in place
    assert (await world.feature(), await world.batches()) == ("delivering", ["failed"])

    sent_back = await world.revise(Kept("S1"), GREETS)
    await world.decide(sent_back, "request_changes", reason="not like this")
    await world.features.start()
    assert (await world.feature(), await world.batches()) == ("delivering", ["failed"])
    assert await revisions_owed(world) == ["analyst"]

    # An ordinary story after the accepted ones: there is no rejected story to correct.
    await world.decide(await world.revise(Kept("S1"), GREETS))
    assert await revisions_owed(world) == []
    assert await world.story("S2") is StoryState.RELEASED
    await world.deliver("G", "S2", GREETING, {"greeting.txt": "hello\n"})
    await world.seal("S2")
    await world.assess("G prints hello", "S2")
    await world.decide(await world.gate())

    assert await world.feature() == "completed"
    summary = await world.summary()
    assert summary is not None
    assert [story["story_id"] for story in summary["stories"]] == ["S1", "S2"]
    assert summary["roadmap_revision"] == 3


async def test_a_roadmap_is_not_proposed_while_the_feature_is_being_checked_or_waits_at_its_gate(
    world: CompletionWorld,
) -> None:
    await failed_on_trunk(world)

    with pytest.raises(ActionRefused, match="final check"):
        await world.revise(Kept("S1"), GREETS)

    assert await world.column("SELECT count(*) FROM roadmaps WHERE state = 'proposed'") == [0]
