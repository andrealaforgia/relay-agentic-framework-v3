"""Pausing, resuming and cancelling a feature: nothing new starts once a closure commits,
what was already observed is kept, and a push authorised before it still runs its course."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application import behaviours as behaviours_module
from relay.application import integration as integration_module
from relay.application import stories as stories_module
from relay.application.integration import IntegrationRefused, Outcome
from relay.application.stories import StoryDraft
from relay.domain.behaviour import BehaviourState
from relay.domain.story import ActionRefused, HumanAction, Receipt, StoryState
from relay.git.checkout import RemoteUnavailable
from relay.git.mirror import Mirror
from relay.testing.oracle import ContainerIsolation
from tests.integration.fixtures import (
    ANALYST,
    CONTRACT,
    PRODUCT_OWNER,
    VAT_APPLIES_STANDARD_RATE,
    VERIFIER,
    HeldAtABarrier,
    announce_wanted_work_again,
    held_at_the_barrier,
    prepare_review,
    run_green,
)
from tests.integration.story_world import GREETS, STANDARD_RATE, StoryWorld, at_the_story_gate
from tests.integration.trunk_world import PROJECT, ZERO_STAYS_ZERO


@pytest.fixture
async def world(database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> StoryWorld:
    world = StoryWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    return world


async def test_nothing_new_starts_on_a_paused_feature_by_any_route(world: StoryWorld) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.behaviours.propose(
        PROJECT, "S1", "B1", "standard rate", ["AC1"], basis=0, story_revision=1
    )
    revision, listed = await world.stories.revisions_of(PROJECT, "S1")

    assert await world.close("pause", reason="budget review") == Receipt("effective", "paused")

    for start in (
        world.behaviours.propose(
            PROJECT, "S1", "B2", "one more", ["AC1"], basis=0, story_revision=1
        ),
        world.behaviours.begin_test_design(PROJECT, "B1"),
        world.behaviours.register_acceptance_test(PROJECT, "B1", CONTRACT, basis=0),
        world.behaviours.run_red(PROJECT, "B1"),
        run_green(world.behaviours, PROJECT, "B1", 1),
        world.behaviours.reopen_red(VERIFIER, PROJECT, "B1", "runner upgraded"),
        world.integrate("B1"),
        world.integration.retry_integration(PROJECT, "B1", "try again"),
        world.seal(),
        world.assess("looks fine"),
        world.stories.publish_roadmap(ANALYST, PROJECT, "F1", [GREETS], basis=0),
    ):
        with pytest.raises(ActionRefused, match="paused"):
            await start
    assert await world.stories.revisions_of(PROJECT, "S1") == (revision, listed)
    assert await world.behaviours.state_of(PROJECT, "B1") is BehaviourState.PROPOSED

    assert await world.close("resume") == Receipt("effective", "resumed")
    await world.behaviours.begin_test_design(PROJECT, "B1")


async def test_a_gate_waits_through_a_pause_and_is_then_decided_once(world: StoryWorld) -> None:
    gate, _ = await at_the_story_gate(world)
    await world.close("pause", reason="on holiday")
    approval = HumanAction(PRODUCT_OWNER, "the-approval", str(gate), "approve")

    with pytest.raises(ActionRefused, match="paused"):
        await world.stories.decide(approval)

    assert await world.column("SELECT count(*) FROM human_actions WHERE action = 'approve'") == [
        1  # the roadmap's, from before
    ]
    assert await world.gate() == gate
    assert await world.story("S2") is StoryState.PLANNED
    await world.close("resume")
    first = await world.stories.decide(approval)
    await world.close("pause", reason="again")

    assert first == await world.stories.decide(approval) == Receipt("effective", "approved")
    assert await world.story("S2") is StoryState.RELEASED


async def test_cancelling_at_an_open_gate_frees_the_project_for_the_feature_waiting(
    world: StoryWorld,
) -> None:
    gate, candidate = await at_the_story_gate(world)
    waiting = StoryDraft("T1", "Another feature", "something else", {"AC1": "it works"})
    await world.decide(await world.roadmap(waiting, feature="F2"))
    assert await world.story("T1") is StoryState.PLANNED

    receipt = await world.close("cancel", reason="no longer wanted")

    assert receipt == Receipt("effective", "cancelled")
    assert (await world.story("S1"), await world.story("S2")) == (
        StoryState.CANCELLED,
        StoryState.CANCELLED,
    )
    assert await world.story("T1") is StoryState.RELEASED
    assert (await world.events())[-2:] == ["feature.cancelled", "story.released"]
    assert await world.stories.inbox(PROJECT) == []
    assert (await world.decide(gate)).disposition == "stale"
    assert world.trunk() == candidate
    for late in ("resume", "pause"):
        payload = {} if late == "resume" else {"reason": "too late"}
        assert (await world.close(late, **payload)).disposition == "stale"


async def test_a_paused_feature_keeps_the_projects_place(world: StoryWorld) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE))
    second = await world.roadmap(GREETS, feature="F2")
    await world.close("pause", reason="thinking")

    await world.decide(second)

    assert (await world.story("S1"), await world.story("S2")) == (
        StoryState.RELEASED,
        StoryState.PLANNED,
    )
    await world.close("cancel", reason="dropped")
    assert await world.story("S2") is StoryState.RELEASED


async def test_closing_and_reopening_are_a_persons_exact_and_repeatable(world: StoryWorld) -> None:
    await world.roadmap(STANDARD_RATE)
    pause = HumanAction(PRODUCT_OWNER, "p1", "F1", "pause", {"reason": "thinking"})

    with pytest.raises(ActionRefused, match="human"):
        await world.features.close(
            PROJECT, HumanAction(ANALYST, "x", "F1", "pause", {"reason": "r"})
        )
    with pytest.raises(ActionRefused, match="only that"):
        await world.features.close(PROJECT, HumanAction(PRODUCT_OWNER, "x", "F1", "pause", {}))
    with pytest.raises(ActionRefused, match="not something"):
        await world.features.close(PROJECT, HumanAction(PRODUCT_OWNER, "x", "F1", "approve", {}))
    assert (await world.close("resume")).disposition == "stale"
    first = await world.features.close(PROJECT, pause)

    assert first == await world.features.close(PROJECT, pause) == Receipt("effective", "paused")
    assert (await world.close("pause", reason="again")).disposition == "stale"
    with pytest.raises(ActionRefused, match="already used"):
        await world.features.close(PROJECT, HumanAction(PRODUCT_OWNER, "p1", "F1", "resume", {}))
    assert (await world.events()).count("feature.paused") == 1
    [paused] = await world.column("SELECT payload FROM events WHERE event_type = 'feature.paused'")
    assert isinstance(paused, dict)
    assert (paused["by"], paused["said"]) == ("product-owner", {"reason": "thinking"})


@pytest.mark.parametrize(
    ("closure", "batch", "outcome"),
    [("pause", "held", Outcome.HELD), ("cancel", "abandoned", Outcome.SUPERSEDED)],
)
async def test_checks_that_finish_after_a_closure_authorise_nothing(
    world: StoryWorld, closure: str, batch: str, outcome: Outcome
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.integration.require_check(PROJECT, ZERO_STAYS_ZERO)
    base = await world.awaiting_builder("B1")
    await world.reviewed("B1", on=base)
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    running = asyncio.create_task(world.integration_with(barrier).integrate(PROJECT, "B1", "once"))
    await held_at_the_barrier(running, barrier)

    await world.close(closure, reason="stop")
    barrier.release.set()
    receipt = await running

    assert receipt.outcome is outcome
    assert closure[:5] in receipt.reason
    assert world.trunk() == base
    assert await world.column("SELECT count(*) FROM integration_intents") == [0]
    assert await world.column("SELECT state FROM check_batches") == [batch]
    assert await world.column("SELECT count(*) FROM executions WHERE purpose = 'check'") == [1]
    assert await world.integration.integrate(PROJECT, "B1", "once") == receipt


async def test_a_push_authorised_before_a_pause_still_lands_and_opens_no_gate_until_resume(
    world: StoryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    base = await world.awaiting_builder("B1")
    candidate = await world.reviewed("B1", on=base)
    await world.seal()
    await world.assess("B1 will show 120 for 100")

    async def unanswered(*_: Any, **__: Any) -> None:
        raise RemoteUnavailable("no route to host")

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "_push", unanswered)
        assert (await world.integrate("B1")).outcome is Outcome.PENDING
    await world.close("pause", reason="stop")

    assert await world.integration.reconcile() == 1

    assert world.trunk() == candidate
    assert await world.behaviours.state_of(PROJECT, "B1") is BehaviourState.INTEGRATED
    assert await world.story("S1") is StoryState.RELEASED
    assert await world.stories.inbox(PROJECT) == []
    assert (await world.events()).count("behaviour.integrated") == 1

    await world.close("resume")

    assert await world.story("S1") is StoryState.AWAITING_APPROVAL
    assert (await world.events())[-2:] == ["feature.resumed", "story.gate_opened"]


async def test_a_review_cannot_be_started_on_a_paused_feature_even_with_its_capability(
    world: StoryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    base = await world.awaiting_builder("B1")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    assert (await run_green(world.behaviours, PROJECT, "B1", 1)).is_valid
    assert await world.reviews.publish_candidates() == 1
    [capability, _] = await world.column(
        "SELECT envelope->'payload'->>'capability' FROM outbox "
        "WHERE envelope->>'event_type' = 'review.requested' ORDER BY position"
    )
    real = Mirror.export

    async def paused_during_the_export(self: Mirror, *arguments: Any) -> Path:
        tree = await real(self, *arguments)
        await world.close("pause", reason="stop")
        return tree

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "export", paused_during_the_export)
        with pytest.raises(ActionRefused, match="paused"):
            await prepare_review(world.reviews, str(capability), world.tmp_path / "tree-1")
    with pytest.raises(ActionRefused, match="paused"):
        await prepare_review(world.reviews, str(capability), world.tmp_path / "tree-2")

    assert await world.column("SELECT count(*) FROM review_checkouts") == [0]
    assert not (world.tmp_path / "tree-2").exists()  # refused before any tree is fetched
    async with world.database.connection() as connection:
        await connection.execute("UPDATE outbox SET published_at = now()")
    assert await announce_wanted_work_again(world.database, PROJECT) == 0
    await world.close("resume")
    assert await announce_wanted_work_again(world.database, PROJECT) == 2
    await prepare_review(world.reviews, str(capability), world.tmp_path / "tree-3")
    assert await world.column("SELECT count(*) FROM review_checkouts") == [1]


async def test_a_candidate_verified_just_before_a_pause_is_not_announced_until_resume(
    world: StoryWorld,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    base = await world.awaiting_builder("B1")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    assert (await run_green(world.behaviours, PROJECT, "B1", 1)).is_valid
    await world.close("pause", reason="stop")

    assert await world.reviews.publish_candidates() == 0
    assert await announce_wanted_work_again(world.database, PROJECT) == 0

    assert await world.column("SELECT publication FROM review_cycles") == ["pending"]
    await world.close("resume")  # the pass that follows a resume publishes it
    assert await world.column("SELECT publication FROM review_cycles") == ["published"]


@pytest.mark.parametrize("first", ["the approval", "the pause"])
async def test_a_pause_and_the_release_of_a_story_cannot_cross(
    world: StoryWorld, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    gate = await world.roadmap(STANDARD_RATE)
    if first == "the pause":
        await world.close("pause", reason="stop")
        with pytest.raises(ActionRefused, match="paused"):
            await world.decide(gate)
        assert not await world.exists("S1")
        return
    holding, go_on = asyncio.Event(), asyncio.Event()
    real = stories_module._locked_project

    async def held_inside_the_approval(*arguments: Any) -> None:
        await real(*arguments)
        if not holding.is_set():
            holding.set()
            await go_on.wait()

    monkeypatch.setattr(stories_module, "_locked_project", held_inside_the_approval)
    approving = asyncio.create_task(world.decide(gate))
    await holding.wait()
    pausing = asyncio.create_task(world.close("pause", reason="stop"))
    await world.until_someone_waits_for_a_lock()
    go_on.set()

    assert (await approving).disposition == "effective"
    assert (await pausing).disposition == "effective"
    assert (await world.events())[-2:] == ["story.released", "feature.paused"]
    with pytest.raises(ActionRefused, match="paused"):
        await world.behaviours.propose(
            PROJECT, "S1", "B1", "standard rate", ["AC1"], basis=0, story_revision=1
        )


async def test_a_pause_and_the_first_roadmap_cannot_cross(
    world: StoryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.stories.open_feature(PRODUCT_OWNER, PROJECT, "F1", "prices need VAT")
    holding, go_on = asyncio.Event(), asyncio.Event()
    real = stories_module._locked_project

    async def held_inside_the_publication(*arguments: Any) -> None:
        await real(*arguments)
        if not holding.is_set():
            holding.set()
            await go_on.wait()

    monkeypatch.setattr(stories_module, "_locked_project", held_inside_the_publication)
    publishing = asyncio.create_task(
        world.stories.publish_roadmap(ANALYST, PROJECT, "F1", [STANDARD_RATE], basis=0)
    )
    await holding.wait()
    pausing = asyncio.create_task(world.close("pause", reason="stop"))
    await world.until_someone_waits_for_a_lock()
    go_on.set()
    gate = await publishing
    await pausing

    assert (await world.events())[-2:] == ["roadmap.gate_opened", "feature.paused"]
    with pytest.raises(ActionRefused, match="paused"):
        await world.decide(gate)
    with pytest.raises(ActionRefused, match="paused"):
        await world.stories.publish_roadmap(ANALYST, PROJECT, "F1", [GREETS], basis=0)


async def test_a_pause_waits_for_work_that_was_admitted_before_it(
    world: StoryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start that has already been told the feature is open must commit before the
    pause does, or it would begin work after the pause was in force."""
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.behaviours.propose(
        PROJECT, "S1", "B1", "standard rate", ["AC1"], basis=0, story_revision=1
    )
    admitted, go_on = asyncio.Event(), asyncio.Event()
    real = behaviours_module.require_story_at_work

    async def held_once_admitted(*arguments: Any, **options: Any) -> None:
        await real(*arguments, **options)
        admitted.set()
        await go_on.wait()

    monkeypatch.setattr(behaviours_module, "require_story_at_work", held_once_admitted)
    starting = asyncio.create_task(world.behaviours.begin_test_design(PROJECT, "B1"))
    await admitted.wait()
    pausing = asyncio.create_task(world.close("pause", reason="stop"))
    await world.until_someone_waits_for_a_lock()
    go_on.set()
    await starting
    await pausing

    assert (await world.events())[-2:] == ["behaviour.test_design_started", "feature.paused"]


async def test_a_pause_waits_for_an_intent_that_was_admitted_before_it(
    world: StoryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the boundary for trunk: the intent was let through first, so it
    exists, and the push it owns goes ahead."""
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    base = await world.awaiting_builder("B1")
    candidate = await world.reviewed("B1", on=base)
    admitted, go_on = asyncio.Event(), asyncio.Event()
    real = integration_module.closure_of_behaviour

    async def held_once_admitted(*arguments: Any) -> Any:
        closed_by = await real(*arguments)
        admitted.set()
        await go_on.wait()
        return closed_by

    monkeypatch.setattr(integration_module, "closure_of_behaviour", held_once_admitted)
    integrating = asyncio.create_task(world.integrate("B1"))
    await admitted.wait()
    pausing = asyncio.create_task(world.close("pause", reason="stop"))
    await world.until_someone_waits_for_a_lock()
    go_on.set()

    assert (await integrating).outcome is Outcome.INTEGRATED
    await pausing
    assert world.trunk() == candidate
    events = await world.events()
    assert events.index("integration.authorised") < events.index("feature.paused")


async def test_a_pause_that_lands_while_a_candidate_is_being_published_holds_its_reviews(
    world: StoryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    base = await world.awaiting_builder("B1")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    assert (await run_green(world.behaviours, PROJECT, "B1", 1)).is_valid
    real = Mirror.publish

    async def paused_during_the_push(self: Mirror, *arguments: Any) -> None:
        await real(self, *arguments)
        await world.close("pause", reason="stop")

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "publish", paused_during_the_push)
        assert await world.reviews.publish_candidates() == 0

    assert await world.column("SELECT publication FROM review_cycles") == ["pending"]
    assert await world.column(
        "SELECT count(*) FROM outbox WHERE envelope->>'event_type' = 'review.requested'"
    ) == [0]
    await world.close("resume")  # the pass that follows a resume publishes it
    assert await world.column("SELECT publication FROM review_cycles") == ["published"]


async def test_a_candidate_stays_off_the_repository_while_its_feature_is_paused(
    world: StoryWorld,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    base = await world.awaiting_builder("B1")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    assert (await run_green(world.behaviours, PROJECT, "B1", 1)).is_valid
    [ref] = await world.column("SELECT candidate_ref FROM review_cycles")
    await world.close("pause", reason="stop")

    assert await world.reviews.publish_candidates() == 0

    assert world.git("for-each-ref", str(ref), at=world.remote) == ""
    await world.close("resume")  # the pass that follows a resume publishes it
    assert await world.column("SELECT publication FROM review_cycles") == ["published"]
    assert world.git("for-each-ref", str(ref), at=world.remote) != ""


async def test_a_place_that_falls_free_does_not_go_to_a_paused_feature(world: StoryWorld) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE))
    await world.decide(await world.roadmap(GREETS, feature="F2"))
    await world.close("pause", feature="F2", reason="not yet")

    await world.close("cancel", reason="dropped")

    assert await world.story("S2") is StoryState.PLANNED
    await world.close("resume", feature="F2")
    assert await world.story("S2") is StoryState.RELEASED


async def test_a_pause_waits_for_a_retry_that_was_admitted_before_it(
    world: StoryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration's transactions take the gate lock like every other: one that has read
    the feature as open commits, or fails, before the pause can."""
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.behaviours.propose(
        PROJECT, "S1", "B1", "standard rate", ["AC1"], basis=0, story_revision=1
    )
    admitted, go_on = asyncio.Event(), asyncio.Event()
    real = integration_module.closure_of_behaviour

    async def held_once_admitted(*arguments: Any) -> Any:
        closed_by = await real(*arguments)
        admitted.set()
        await go_on.wait()
        return closed_by

    monkeypatch.setattr(integration_module, "closure_of_behaviour", held_once_admitted)
    retrying = asyncio.create_task(world.integration.retry_integration(PROJECT, "B1", "again"))
    await admitted.wait()
    pausing = asyncio.create_task(world.close("pause", reason="stop"))
    await world.until_someone_waits_for_a_lock()
    go_on.set()

    with pytest.raises(IntegrationRefused, match="no failed integration"):
        await retrying
    assert (await pausing).disposition == "effective"


async def test_a_request_id_used_in_one_project_acknowledges_nothing_in_another(
    world: StoryWorld, database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    (tmp_path / "other").mkdir()
    other = StoryWorld(database, tmp_path / "other", runner_image, project="zeta")
    await other.open()
    await other.roadmap(STANDARD_RATE)
    await world.roadmap(STANDARD_RATE)
    cancel = HumanAction(PRODUCT_OWNER, "c1", "F1", "cancel", {"reason": "dropped"})
    first = await world.features.close(PROJECT, cancel)

    with pytest.raises(ActionRefused, match="already used"):
        await other.features.close("zeta", cancel)

    assert await world.features.close(PROJECT, cancel) == first == Receipt("effective", "cancelled")
    assert await world.column("SELECT closed_by FROM features WHERE project_id = 'zeta'") == [None]
