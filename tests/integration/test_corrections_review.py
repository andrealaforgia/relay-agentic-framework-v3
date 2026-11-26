"""Histories found in review of roadmap revisions and rejection."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application import decisions as decisions_module
from relay.application import reviews as reviews_module
from relay.application.features import Features
from relay.application.integration import Outcome, hand_on_unfinished_batches
from relay.application.stories import Dropped, Kept, Reworded, StoryDraft, close_feature
from relay.domain.behaviour import BehaviourState
from relay.domain.story import ActionRefused, HumanAction, StoryState
from relay.testing.oracle import ContainerIsolation
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import (
    ANALYST,
    PRODUCT_OWNER,
    VAT_APPLIES_STANDARD_RATE,
    VERIFIER,
    HeldAtABarrier,
    held_at_the_barrier,
    prepare_review,
    run_green,
)
from tests.integration.story_world import GREETS, STANDARD_RATE
from tests.integration.trunk_world import PROJECT, ZERO_STAYS_ZERO

REWORDED_S2 = Reworded(
    "S2", "A warmer greeting", "greeted by name", {"AC1": "hello, Ada is printed"},
    story_revision=1, story_basis=0,
)  # fmt: skip


@pytest.fixture
async def world(database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> DecisionWorld:
    world = DecisionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    return world


async def reject(world: DecisionWorld, story: str = "S1") -> None:
    revision, _ = await world.stories.revisions_of(PROJECT, story)
    world.requests += 1
    payload = {"reason": "no", "revision": str(revision), "basis": str(await world.basis(story))}
    receipt = await world.stories.reject(
        PROJECT, HumanAction(PRODUCT_OWNER, f"reject-{world.requests}", story, "reject", payload)
    )
    assert receipt.disposition == "effective"


async def test_a_behaviour_is_proposed_against_the_story_revision_its_author_read(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    """The Verifier reads a planned story. A revision rewords its criterion, keeping the
    criterion's name, and the story is released. The proposal written for the old
    wording must not start work."""
    world = DecisionWorld(database, tmp_path, runner_image)
    await world.open()
    other = StoryDraft("T1", "Another feature", "something else", {"AC1": "it works"})
    await world.decide(await world.roadmap(other, feature="F0"))  # T1 holds the place
    await world.decide(await world.roadmap(GREETS))
    read_by_the_verifier, _ = await world.stories.revisions_of(PROJECT, "S2")
    await world.decide(await world.revise(REWORDED_S2))
    await world.close("cancel", feature="F0", reason="dropped")
    assert await world.story("S2") is StoryState.RELEASED
    before = await world.events()

    with pytest.raises(ActionRefused, match="has moved on"):
        await world.behaviours.propose(VERIFIER, 
            PROJECT, "S2", "B1", "prints hello", ["AC1"], basis=0,
            story_revision=read_by_the_verifier,
        )  # fmt: skip

    assert await world.column("SELECT count(*) FROM behaviours") == [0]
    assert await world.events() == before
    await world.behaviours.propose(
        VERIFIER, PROJECT, "S2", "B1", "prints hello, Ada", ["AC1"], basis=0, story_revision=2
    )


@pytest.mark.parametrize("roadmap_write", ["its approval", "its publication"])
async def test_reconfirming_a_story_and_a_roadmap_that_edits_it_do_not_deadlock(
    world: DecisionWorld, monkeypatch: pytest.MonkeyPatch, roadmap_write: str
) -> None:
    """The reconfirmation has the story and wants the project. The roadmap write wants
    the project and then the story. Without one lock over both, one of them is killed."""
    await world.answer(await world.ask("S2"))
    owed = next(each for each in await world.owed() if each["story_id"] == "S2")
    grant = await world.claims.claim(ANALYST, owed["assignment_id"])
    dropping = [Kept("S1"), Dropped("S2", story_revision=1, story_basis=1)]
    gate = await world.revise(*dropping) if roadmap_write == "its approval" else None
    holding, go_on = asyncio.Event(), asyncio.Event()
    real = decisions_module._locked_project

    async def held_with_the_story(*arguments: Any) -> None:
        if not holding.is_set():
            holding.set()
            await go_on.wait()
        await real(*arguments)

    monkeypatch.setattr(decisions_module, "_locked_project", held_with_the_story)
    reconfirming = asyncio.create_task(
        world.decisions.reconfirm_story(ANALYST, PROJECT, "S2", grant=grant, basis=1, revision=1)
    )
    await holding.wait()
    writing = asyncio.create_task(world.decide(gate) if gate else world.revise(*dropping))
    await world.until_someone_waits_for_a_lock()
    go_on.set()

    await reconfirming
    written = await writing
    if gate is None:
        assert (await world.stories.roadmap_of(PROJECT, "F1")).proposed == 2
    else:
        assert written.disposition == "effective"
        assert await world.story("S2") is StoryState.WITHDRAWN


async def test_two_features_proposing_a_story_of_the_same_name_do_not_both_get_it(
    world: DecisionWorld,
) -> None:
    refunds = StoryDraft("S3", "Refunds", "a refund is shown", {"AC1": "120 shows 120"})
    ours = await world.revise(Kept("S1"), Kept("S2"), refunds)
    theirs = await world.roadmap(refunds, feature="F2")
    assert (await world.decide(ours)).disposition == "effective"

    approval = HumanAction(PRODUCT_OWNER, "approve-theirs", str(theirs), "approve")
    late = await world.stories.decide(approval)

    assert late.disposition == "stale"
    assert "S3" in late.outcome
    assert await world.stories.decide(approval) == late
    assert await world.column("SELECT feature_id FROM stories WHERE story_id = 'S3'") == ["F1"]
    assert (await world.stories.roadmap_of(PROJECT, "F2")).effective is None


async def test_a_conflict_seen_as_a_story_is_rejected_blocks_nothing(
    world: DecisionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = await world.awaiting_builder("B1")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    assert (await run_green(world.behaviours, PROJECT, "B1", 1)).is_valid
    assert await world.reviews.publish_candidates() == 1
    [task_id] = await world.column("SELECT task_id FROM review_tasks WHERE role = 'qa'")
    real_resolve = reviews_module.resolve_ref
    rejected: list[bool] = []

    async def moved_while_the_story_is_rejected(repository: str, ref: str, **options: Any) -> Any:
        if not rejected and "candidates" in ref:
            rejected.append(True)
            await reject(world)
            return base  # not the candidate
        return await real_resolve(repository, ref, **options)

    monkeypatch.setattr(reviews_module, "resolve_ref", moved_while_the_story_is_rejected)
    with pytest.raises(Exception, match="no longer names|cancelled|not being worked on"):
        await prepare_review(world.reviews, task_id, world.tmp_path / "tree")

    assert await world.column("SELECT state FROM review_cycles") == ["open"]
    assert await world.state("B1") is BehaviourState.REVIEW
    assert "review.cycle_blocked" not in await world.events()
    # What was seen is on the record all the same, for whoever looks after a restart.
    assert (await world.events()).count("review.conflict_observed") == 1


async def test_rejecting_a_story_ends_the_request_whose_checks_were_waiting_to_run_again(
    world: DecisionWorld,
) -> None:
    await world.integration.require_check(PROJECT, ZERO_STAYS_ZERO)
    base = await world.awaiting_builder("B1")
    await world.reviewed("B1", on=base)
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    running = asyncio.create_task(world.integration_with(barrier).integrate(PROJECT, "B1", "once"))
    await held_at_the_barrier(running, barrier)
    await world.close("pause", reason="one moment")
    barrier.release.set()
    assert (await running).outcome is Outcome.HELD
    resume = HumanAction(PRODUCT_OWNER, "resume-1", "F1", "resume", {})
    await close_feature(world.database, PROJECT, resume)  # the pass has not run
    async with world.database.connection() as connection, connection.transaction():
        await hand_on_unfinished_batches(connection, PROJECT, starting=False)
    assert await world.column("SELECT state FROM check_batches") == ["interrupted"]

    await reject(world)

    assert await world.column("SELECT state FROM check_batches") == ["abandoned"]
    await Features(world.database, world.reviews, world.integration).start()
    again = await world.integration.integrate(PROJECT, "B1", "once")
    assert again.outcome is Outcome.SUPERSEDED
    assert world.trunk() == base


async def test_checks_that_finish_after_their_story_was_rejected_say_so(
    world: DecisionWorld,
) -> None:
    await world.integration.require_check(PROJECT, ZERO_STAYS_ZERO)
    base = await world.awaiting_builder("B1")
    await world.reviewed("B1", on=base)
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    running = asyncio.create_task(world.integration_with(barrier).integrate(PROJECT, "B1", "once"))
    await held_at_the_barrier(running, barrier)

    await reject(world)
    barrier.release.set()
    receipt = await running

    assert receipt.outcome is Outcome.SUPERSEDED
    assert "S1 was rejected" in receipt.reason and "cancelled" not in receipt.reason
    assert world.trunk() == base
    assert await world.column("SELECT count(*) FROM integration_intents") == [0]


async def test_a_reorder_tells_the_person_what_moved_and_what_each_correction_corrects(
    world: DecisionWorld,
) -> None:
    refunds = StoryDraft("S3", "Refunds", "a refund is shown", {"AC1": "120 shows 120"})
    await world.decide(await world.revise(Kept("S1"), Kept("S2"), refunds))

    await world.revise(Kept("S1"), Kept("S3"), Kept("S2"))

    [item] = await world.stories.inbox(PROJECT)
    assert item.manifest["changes"]["moved"] == [
        {"story_id": "S3", "from": 3, "to": 2},
        {"story_id": "S2", "from": 2, "to": 3},
    ]
    assert item.manifest["changes"]["kept"] == ["S1", "S3", "S2"]


async def test_a_story_that_was_dropped_stays_withdrawn_when_its_feature_is_cancelled(
    world: DecisionWorld,
) -> None:
    await world.decide(
        await world.revise(Kept("S1"), Dropped("S2", story_revision=1, story_basis=0))
    )

    await world.close("cancel", reason="dropped")

    assert (await world.story("S1"), await world.story("S2")) == (
        StoryState.CANCELLED,
        StoryState.WITHDRAWN,
    )


async def test_reconfirming_a_story_and_reconfirming_a_proposal_that_edits_it_do_not_deadlock(
    world: DecisionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.revise(Kept("S1"), REWORDED_S2)
    await world.answer(await world.ask(story=None))
    owed = await world.owed()
    proposal = next(each for each in owed if each["revision"] == 2)
    story = next(each for each in owed if each["story_id"] == "S2")
    for_the_proposal = await world.claims.claim(ANALYST, proposal["assignment_id"])
    for_the_story = await world.claims.claim(ANALYST, story["assignment_id"])
    holding, go_on = asyncio.Event(), asyncio.Event()
    real = decisions_module._locked_project

    async def held_with_the_story(*arguments: Any) -> None:
        if not holding.is_set():
            holding.set()
            await go_on.wait()
        await real(*arguments)

    monkeypatch.setattr(decisions_module, "_locked_project", held_with_the_story)
    the_story = asyncio.create_task(
        world.decisions.reconfirm_story(
            ANALYST, PROJECT, "S2", grant=for_the_story, basis=1, revision=1
        )
    )
    await holding.wait()
    the_proposal = asyncio.create_task(
        world.decisions.reconfirm_roadmap(
            ANALYST, PROJECT, "F1", grant=for_the_proposal, basis=1, revision=2
        )
    )
    await world.until_someone_waits_for_a_lock()
    go_on.set()

    await the_story
    with pytest.raises(ActionRefused, match="has to be replaced"):
        await the_proposal
    assert await world.column("SELECT current_on FROM stories WHERE story_id = 'S2'") == [1]


async def test_a_new_story_is_shown_correcting_only_what_its_proposal_says(
    world: DecisionWorld,
) -> None:
    """Feature F2 proposes an ordinary new story C1. Before it is approved, feature F1
    creates a C1 of its own as the correction of a rejected story. When F2's proposal is
    shown again, its C1 corrects nothing."""
    ordinary = StoryDraft("C1", "A catalogue", "items are listed", {"AC1": "it lists"})
    correction = StoryDraft(
        "C1", "The rate, corrected", "the gross price is shown", {"AC1": "120"}, corrects="S1"
    )
    await world.roadmap(ordinary, feature="F2")
    await reject(world, "S1")
    await world.decide(await world.revise(Kept("S1"), Kept("S2"), correction))
    await world.answer(await world.ask(story=None, feature="F2"))

    await world.reconfirm(next(each for each in await world.owed() if each["feature_id"] == "F2"))

    [shown] = [
        item for item in await world.stories.inbox(PROJECT) if item.manifest["feature_id"] == "F2"
    ]
    assert shown.manifest["stories"] == [
        {"story_id": "C1", "title": "A catalogue", "outcome": "items are listed",
         "criteria": {"AC1": "it lists"}}
    ]  # fmt: skip
    ours = await world.revise(Kept("S1"), Kept("C1"), Kept("S2"))
    [reorder] = [item for item in await world.stories.inbox(PROJECT) if item.gate_id == ours]
    assert [story.get("corrects") for story in reorder.manifest["stories"]] == [None, "S1", None]


async def test_checks_closed_at_a_restart_after_their_story_was_rejected_say_so(
    world: DecisionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.integration.require_check(PROJECT, ZERO_STAYS_ZERO)
    base = await world.awaiting_builder("B1")
    await world.reviewed("B1", on=base)
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    dying = world.integration_with(barrier)

    async def nothing(self: Any, batch_id: Any) -> None:
        return None

    monkeypatch.setattr(type(dying), "_abandon", nothing)  # a process that is killed tidies nothing
    running = asyncio.create_task(dying.integrate(PROJECT, "B1", "once"))
    await held_at_the_barrier(running, barrier)
    await reject(world)
    running.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await running

    await Features(world.database, world.reviews, world.integration).start()

    again = await world.integration.integrate(PROJECT, "B1", "once")
    assert again.outcome is Outcome.SUPERSEDED
    assert "S1 was rejected" in again.reason and "cancelled" not in again.reason


async def test_a_reviewer_is_told_the_story_was_rejected_not_that_the_feature_was_cancelled(
    world: DecisionWorld,
) -> None:
    base = await world.awaiting_builder("B1")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    assert (await run_green(world.behaviours, PROJECT, "B1", 1)).is_valid
    assert await world.reviews.publish_candidates() == 1
    [task_id] = await world.column("SELECT task_id FROM review_tasks WHERE role = 'qa'")
    await reject(world)

    with pytest.raises(ActionRefused) as refused:
        await prepare_review(world.reviews, task_id, world.tmp_path / "tree")

    assert "S1 was rejected" in str(refused.value) and "cancelled" not in str(refused.value)
    assert await world.column("SELECT count(*) FROM review_checkouts") == [0]


@pytest.mark.parametrize("first", ["the rejection", "the approval"])
async def test_a_roadmap_approval_and_a_rejection_in_either_order(
    world: DecisionWorld, first: str
) -> None:
    refunds = StoryDraft("S3", "Refunds", "a refund is shown", {"AC1": "120 shows 120"})
    proposal = await world.revise(Kept("S1"), Kept("S2"), refunds)

    if first == "the rejection":
        await reject(world)
        approved = await world.decide(proposal)
        # The proposal has no correction for what is now rejected.
        assert approved.disposition == "stale"
        assert "S1 is rejected and nothing in this roadmap corrects it" in approved.outcome
        assert (await world.stories.roadmap_of(PROJECT, "F1")).effective == 1
    else:
        assert (await world.decide(proposal)).disposition == "effective"
        await reject(world)
        assert (await world.stories.roadmap_of(PROJECT, "F1")).effective == 2

    assert await world.column("SELECT state FROM assignments WHERE kind = 'correct'") == ["wanted"]
    assert await world.story("S2") is StoryState.PLANNED  # ordinary work waits either way
