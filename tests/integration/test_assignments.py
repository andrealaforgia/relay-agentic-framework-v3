"""Assignments: each unit of work Relay wants done is a row of its own, announced by a
command and granted to one worker at a time. A grant is what a worker's first effect has
to name, so work that was replaced, withdrawn or paused cannot start late."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.assignments import Assignments
from relay.application.behaviours import Behaviours
from relay.application.reviews import Review
from relay.application.stories import StoryDraft
from relay.domain.behaviour import BehaviourState
from relay.domain.evidence import GreenRejection, RedRejection
from relay.domain.story import ActionRefused, Principal, PrincipalKind
from relay.git.checkout import CheckoutFailed
from relay.testing.oracle import ContainerIsolation, NativeIsolation, ProcessOracle
from tests.integration.fixtures import (
    ANALYST,
    BUILDER,
    CONTRACT,
    PRODUCT_OWNER,
    VAT_APPLIES_STANDARD_RATE,
    HeldAtABarrier,
    held_at_the_barrier,
)
from tests.integration.story_world import GREETS, INJECTION, STANDARD_RATE, StoryWorld
from tests.integration.trunk_world import PROJECT

ANOTHER_BUILDER = Principal(PrincipalKind.WORKER, "worker-builder-2", role="builder")


@pytest.fixture
async def world(database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> StoryWorld:
    world = StoryWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    return world


async def the_build(world: StoryWorld) -> uuid.UUID:
    """The assignment the Builder was told about, read from the command itself."""
    [announced] = await world.column(
        "SELECT envelope->'payload'->>'assignment_id' FROM outbox "
        "WHERE envelope->>'event_type' = 'build.requested' ORDER BY position DESC LIMIT 1"
    )
    return uuid.UUID(str(announced))


async def assignments(world: StoryWorld) -> list[object]:
    return await world.column("SELECT kind || ':' || state FROM assignments ORDER BY created_at")


async def test_a_verified_red_owes_one_build_and_the_command_says_which(
    world: StoryWorld,
) -> None:
    await world.awaiting_builder("B1")

    assert await assignments(world) == ["build:wanted"]
    assert [str(await the_build(world))] == await world.column(
        "SELECT assignment_id::text FROM assignments"
    )


async def test_a_grant_is_one_workers_and_asking_again_returns_the_same_one(
    world: StoryWorld,
) -> None:
    await world.awaiting_builder("B1")
    claims = Assignments(world.database)
    build = await the_build(world)

    grant = await claims.claim(BUILDER, build)

    assert await claims.claim(BUILDER, build) == grant
    assert await assignments(world) == ["build:granted"]
    assert (await world.events())[-1] == "assignment.granted"
    with pytest.raises(ActionRefused, match="already granted"):
        await claims.claim(ANOTHER_BUILDER, build)
    with pytest.raises(ActionRefused, match="for a builder"):
        await claims.claim(ANALYST, build)
    with pytest.raises(ActionRefused, match="for a builder"):
        await claims.claim(PRODUCT_OWNER, build)
    a_person_in_the_role = Principal(PrincipalKind.HUMAN, "product-owner", role="builder")
    with pytest.raises(ActionRefused, match="for a builder"):
        await claims.claim(a_person_in_the_role, build)
    with pytest.raises(ActionRefused, match="no such assignment"):
        await claims.claim(BUILDER, uuid.uuid7())
    assert (await world.events()).count("assignment.granted") == 1


async def test_a_build_whose_red_was_reopened_can_no_longer_be_claimed(world: StoryWorld) -> None:
    await world.awaiting_builder("B1")
    build = await the_build(world)

    await world.behaviours.reopen_red(PROJECT, "B1", "the runner was upgraded")

    assert await assignments(world) == ["build:retired"]
    with pytest.raises(ActionRefused, match="no longer wanted"):
        await Assignments(world.database).claim(BUILDER, build)


async def test_nothing_is_granted_on_a_paused_feature_until_it_resumes(world: StoryWorld) -> None:
    await world.awaiting_builder("B1")
    build = await the_build(world)
    await world.close("pause", reason="budget review")

    with pytest.raises(ActionRefused, match="paused"):
        await Assignments(world.database).claim(BUILDER, build)

    assert await assignments(world) == ["build:wanted"]
    await world.close("resume")
    await Assignments(world.database).claim(BUILDER, build)


async def test_a_pause_takes_back_a_grant_that_has_done_nothing_yet(world: StoryWorld) -> None:
    await world.awaiting_builder("B1")
    claims = Assignments(world.database)
    build = await the_build(world)
    before = await claims.claim(BUILDER, build)

    await world.close("pause", reason="budget review")

    assert await assignments(world) == ["build:wanted"]
    assert (await world.events())[-2:] == ["assignment.grant_revoked", "feature.paused"]
    await world.close("resume")
    assert await claims.claim(ANOTHER_BUILDER, build) != before


async def test_a_pause_leaves_the_grants_of_other_features_alone(world: StoryWorld) -> None:
    await world.awaiting_builder("B1")
    claims = Assignments(world.database)
    grant = await claims.claim(BUILDER, await the_build(world))
    another = StoryDraft("T1", "Another feature", "something else", {"AC1": "it works"})
    await world.roadmap(another, feature="F2")

    await world.close("pause", feature="F2", reason="not yet")

    assert await assignments(world) == ["build:granted"]
    assert await claims.claim(BUILDER, await the_build(world)) == grant
    await world.close("cancel", feature="F2", reason="dropped")
    assert await claims.claim(BUILDER, await the_build(world)) == grant


async def test_cancelling_a_feature_withdraws_its_work(world: StoryWorld) -> None:
    await world.awaiting_builder("B1")
    build = await the_build(world)
    await Assignments(world.database).claim(BUILDER, build)

    await world.close("cancel", reason="no longer wanted")

    assert await assignments(world) == ["build:retired"]
    with pytest.raises(ActionRefused, match="cancelled"):
        await Assignments(world.database).claim(BUILDER, build)


async def test_an_accepted_green_completes_the_build_and_changes_requested_owe_a_remediation(
    world: StoryWorld,
) -> None:
    base = await world.awaiting_builder("B1")
    build = await the_build(world)

    await world.reviewed("B1", on=base, reviews={"sentinel": Review(findings=(INJECTION,))})

    assert await assignments(world) == ["build:done", "remediate:wanted"]
    remediation = await the_build(world)
    assert remediation != build
    [spent] = await world.column("SELECT grant_id FROM assignments WHERE state = 'done'")
    assert isinstance(spent, uuid.UUID)
    with pytest.raises(ActionRefused, match="not the current one"):
        await world.behaviours.run_green(PROJECT, "B1", 2, spent)
    with pytest.raises(ActionRefused, match="no longer wanted"):
        await Assignments(world.database).claim(BUILDER, build)
    await Assignments(world.database).claim(BUILDER, remediation)


async def test_a_green_run_has_to_name_the_grant_it_was_given(world: StoryWorld) -> None:
    base = await world.awaiting_builder("B1")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    claims = Assignments(world.database)
    build = await the_build(world)
    taken_back = await claims.claim(BUILDER, build)
    await world.close("pause", reason="budget review")
    await world.close("resume")

    for not_current in (uuid.uuid7(), taken_back):
        with pytest.raises(ActionRefused, match="not the current one"):
            await world.behaviours.run_green(PROJECT, "B1", 1, not_current)

    assert await world.column("SELECT count(*) FROM green_runs") == [0]
    current = await claims.claim(BUILDER, build)
    assert (await world.behaviours.run_green(PROJECT, "B1", 1, current)).is_valid
    assert await world.column("SELECT state FROM green_runs") == ["finished"]


def held(world: StoryWorld) -> tuple[HeldAtABarrier, Behaviours]:
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    return barrier, Behaviours(world.database, world.mirrors, ProcessOracle(barrier))


async def test_a_pause_leaves_a_green_run_that_has_started_to_finish_and_be_judged(
    world: StoryWorld,
) -> None:
    base = await world.awaiting_builder("B1")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    grant = await Assignments(world.database).claim(BUILDER, await the_build(world))
    barrier, behaviours = held(world)
    running = asyncio.create_task(behaviours.run_green(PROJECT, "B1", 1, grant))
    await held_at_the_barrier(running, barrier)

    await world.close("pause", reason="budget review")
    assert await assignments(world) == ["build:granted"]
    barrier.release.set()

    assert (await running).is_valid
    assert await assignments(world) == ["build:done"]
    assert await world.state("B1") is BehaviourState.REVIEW


async def test_a_second_green_that_settles_after_a_remediation_is_owed_changes_nothing(
    world: StoryWorld,
) -> None:
    base = await world.awaiting_builder("B1")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    grant = await Assignments(world.database).claim(BUILDER, await the_build(world))
    barrier, behaviours = held(world)
    late = asyncio.create_task(behaviours.run_green(PROJECT, "B1", 1, grant))
    await held_at_the_barrier(late, barrier)
    await world.reviewed("B1", on=base, reviews={"sentinel": Review(findings=(INJECTION,))})
    assert await world.state("B1") is BehaviourState.IMPLEMENTING

    barrier.release.set()
    verdict = await late

    assert verdict.rejection is GreenRejection.ASSIGNMENT_SUPERSEDED
    assert await world.state("B1") is BehaviourState.IMPLEMENTING
    assert await world.column("SELECT count(*) FROM review_cycles") == [1]
    assert await world.column("SELECT count(*) FROM executions WHERE purpose = 'green'") == [2]
    assert await assignments(world) == ["build:done", "remediate:wanted"]


async def test_a_green_that_finishes_after_a_cancel_is_kept_as_evidence_only(
    world: StoryWorld,
) -> None:
    base = await world.awaiting_builder("B1")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    grant = await Assignments(world.database).claim(BUILDER, await the_build(world))
    barrier, behaviours = held(world)
    running = asyncio.create_task(behaviours.run_green(PROJECT, "B1", 1, grant))
    await held_at_the_barrier(running, barrier)

    await world.close("cancel", reason="no longer wanted")
    barrier.release.set()

    assert (await running).rejection is GreenRejection.ASSIGNMENT_SUPERSEDED
    assert await world.state("B1") is BehaviourState.IMPLEMENTING
    assert await world.column("SELECT count(*) FROM review_cycles") == [0]
    assert await world.column("SELECT count(*) FROM executions WHERE purpose = 'green'") == [1]
    assert await assignments(world) == ["build:retired"]


async def test_a_red_that_finishes_after_a_cancel_is_kept_as_evidence_only(
    world: StoryWorld,
) -> None:
    await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"])
    await world.behaviours.begin_test_design(PROJECT, "B1")
    await world.behaviours.register_acceptance_test(PROJECT, "B1", CONTRACT)
    barrier, behaviours = held(world)
    running = asyncio.create_task(behaviours.run_red(PROJECT, "B1"))
    await held_at_the_barrier(running, barrier)

    await world.close("cancel", reason="no longer wanted")
    barrier.release.set()

    assert (await running).rejection is RedRejection.SUPERSEDED
    assert await world.state("B1") is BehaviourState.VALIDATING_RED
    assert await world.column("SELECT count(*) FROM executions WHERE purpose = 'red'") == [1]
    assert await assignments(world) == []


async def test_a_red_that_finishes_during_a_pause_is_judged_and_its_build_waits(
    world: StoryWorld,
) -> None:
    await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"])
    await world.behaviours.begin_test_design(PROJECT, "B1")
    await world.behaviours.register_acceptance_test(PROJECT, "B1", CONTRACT)
    barrier, behaviours = held(world)
    running = asyncio.create_task(behaviours.run_red(PROJECT, "B1"))
    await held_at_the_barrier(running, barrier)

    await world.close("pause", reason="budget review")
    barrier.release.set()

    assert (await running).is_valid
    assert await assignments(world) == ["build:wanted"]
    with pytest.raises(ActionRefused, match="paused"):
        await Assignments(world.database).claim(BUILDER, await the_build(world))


async def test_a_green_run_that_never_launches_still_closes_its_record(world: StoryWorld) -> None:
    await world.awaiting_builder("B1")
    grant = await Assignments(world.database).claim(BUILDER, await the_build(world))

    with pytest.raises(CheckoutFailed):  # no attempt was published
        await world.behaviours.run_green(PROJECT, "B1", 1, grant)
    native = Behaviours(world.database, world.mirrors, ProcessOracle(NativeIsolation()))
    refused = await native.run_green(PROJECT, "B1", 1, grant)

    assert refused.rejection is GreenRejection.ISOLATION_NOT_ADMISSIBLE
    assert await world.column("SELECT state FROM green_runs") == ["finished", "finished"]
    assert await assignments(world) == ["build:granted"]
