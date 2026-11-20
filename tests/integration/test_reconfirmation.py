"""Each refusal around the decision basis, reached on its own: a submission written
before a decision, work on something not yet reconfirmed, and a reconfirmation that is
not the owner's, not about what stands now, or not under the grant that holds it."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.behaviours import Behaviours
from relay.application.stories import StoryDraft
from relay.domain.evidence import RedRejection
from relay.domain.story import ActionRefused, StoryState
from relay.testing.oracle import ContainerIsolation, ProcessOracle
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import (
    ANALYST,
    BUILDER,
    CONTRACT,
    VERIFIER,
    HeldAtABarrier,
    held_at_the_barrier,
)
from tests.integration.story_world import GREETS, STANDARD_RATE
from tests.integration.trunk_world import PROJECT, ZERO_STAYS_ZERO


@pytest.fixture
async def world(database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> DecisionWorld:
    world = DecisionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    return world


async def decided(world: DecisionWorld, story: str | None = "S1") -> None:
    await world.answer(await world.ask(story))


async def owed_for(world: DecisionWorld, subject: str, story: str = "S1") -> dict[str, object]:
    [owed] = [
        each
        for each in await world.owed()
        if each["subject"] == subject and each["story_id"] in (story, None)
    ]
    return owed


async def test_a_proposal_names_the_basis_it_was_written_on_and_needs_a_current_story(
    world: DecisionWorld,
) -> None:
    await decided(world)

    with pytest.raises(ActionRefused, match="S1 is not current"):
        await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"], basis=1)
    await world.reconfirm_everything()
    with pytest.raises(ActionRefused, match="written on decision basis 0"):
        await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"], basis=0)

    await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"], basis=1)


async def test_a_contract_names_the_basis_it_was_written_on(world: DecisionWorld) -> None:
    await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"], basis=0)
    await world.behaviours.begin_test_design(PROJECT, "B1")
    await decided(world)
    await world.reconfirm_everything()

    with pytest.raises(ActionRefused, match="written on decision basis 0"):
        await world.behaviours.register_acceptance_test(PROJECT, "B1", CONTRACT, basis=0)

    await world.behaviours.register_acceptance_test(PROJECT, "B1", CONTRACT, basis=1)
    assert (await world.behaviours.run_red(PROJECT, "B1")).is_valid


async def test_a_seal_names_its_basis_and_vouches_only_for_contracts_that_are_current(
    world: DecisionWorld,
) -> None:
    await world.deliver("B1")
    revision, listed = await world.stories.revisions_of(PROJECT, "S1")
    await decided(world)
    await world.reconfirm(await owed_for(world, "story"))

    with pytest.raises(ActionRefused, match="the contract of B1 is not current"):
        await world.stories.seal(
            VERIFIER, PROJECT, "S1", story_revision=revision, list_revision=listed, basis=1
        )
    await world.reconfirm(await owed_for(world, "behaviour"))
    with pytest.raises(ActionRefused, match="written on decision basis 0"):
        await world.stories.seal(
            VERIFIER, PROJECT, "S1", story_revision=revision, list_revision=listed, basis=0
        )

    await world.stories.seal(
        VERIFIER, PROJECT, "S1", story_revision=revision, list_revision=listed, basis=1
    )


async def test_a_candidate_is_not_integrated_on_a_contract_nobody_has_reconfirmed(
    world: DecisionWorld,
) -> None:
    base = await world.awaiting_builder("B1")
    candidate = await world.reviewed("B1", on=base)
    await decided(world)
    await world.reconfirm(await owed_for(world, "story"))

    with pytest.raises(ActionRefused, match="the contract of B1 is not current"):
        await world.integrate("B1")

    assert world.trunk() == base
    await world.reconfirm(await owed_for(world, "behaviour"))
    await world.integrate("B1")
    assert world.trunk() == candidate


async def test_a_red_in_flight_when_a_decision_lands_is_evidence_only_even_if_all_is_reconfirmed(
    world: DecisionWorld,
) -> None:
    await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"], basis=0)
    await world.behaviours.begin_test_design(PROJECT, "B1")
    await world.behaviours.register_acceptance_test(PROJECT, "B1", CONTRACT, basis=0)
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    slow = Behaviours(world.database, world.mirrors, ProcessOracle(barrier))
    running = asyncio.create_task(slow.run_red(PROJECT, "B1"))
    await held_at_the_barrier(running, barrier)
    await decided(world)
    await world.reconfirm_everything()

    barrier.release.set()

    assert (await running).rejection is RedRejection.SUPERSEDED
    assert await world.column("SELECT count(*) FROM assignments WHERE kind = 'build'") == [0]
    assert (await world.behaviours.run_red(PROJECT, "B1")).is_valid


async def test_a_decision_made_without_a_question_takes_back_grants_that_have_done_nothing(
    world: DecisionWorld,
) -> None:
    await world.awaiting_builder("B1")
    [build] = await world.column("SELECT assignment_id FROM assignments")
    await world.claims.claim(BUILDER, build)  # type: ignore[arg-type]
    assumed = await world.decisions.assume(ANALYST, PROJECT, "F1", story_id="S1", text="pounds")

    await world.replace(assumed, "pence")

    assert await world.column("SELECT state FROM assignments WHERE kind = 'build'") == ["wanted"]


async def test_a_story_waits_for_its_roadmap_to_be_reconfirmed_before_it_is_released(
    world: DecisionWorld,
) -> None:
    waiting = StoryDraft("T1", "Another feature", "something else", {"AC1": "it works"})
    await world.decide(await world.roadmap(waiting, feature="F2"))
    assumed = await world.decisions.assume(ANALYST, PROJECT, "F2", text="whole pounds")
    await world.replace(assumed, "pence")
    await world.reconfirm(await owed_for(world, "story", "T1"))
    await world.close("cancel", reason="dropped")  # the place falls free

    assert await world.story("T1") is StoryState.PLANNED

    roadmap = await owed_for(world, "roadmap")
    await world.reconfirm(roadmap)
    assert await world.story("T1") is StoryState.RELEASED


async def test_a_roadmap_is_reconfirmed_by_the_analyst_as_it_stands_on_the_basis_named(
    world: DecisionWorld,
) -> None:
    await decided(world, story=None)
    owed = await owed_for(world, "roadmap")
    grant = await world.claims.claim(ANALYST, owed["assignment_id"])  # type: ignore[arg-type]

    with pytest.raises(ActionRefused, match="for the analyst"):
        await world.decisions.reconfirm_roadmap(
            VERIFIER, PROJECT, "F1", grant=grant, basis=1, revision=1
        )
    with pytest.raises(ActionRefused, match="written on decision basis 0"):
        await world.decisions.reconfirm_roadmap(
            ANALYST, PROJECT, "F1", grant=grant, basis=0, revision=1
        )
    with pytest.raises(ActionRefused, match="is not F1's roadmap"):
        await world.decisions.reconfirm_roadmap(
            ANALYST, PROJECT, "F1", grant=grant, basis=1, revision=2
        )

    await world.decisions.reconfirm_roadmap(
        ANALYST, PROJECT, "F1", grant=grant, basis=1, revision=1
    )
    with pytest.raises(ActionRefused, match="not the current one"):
        await world.decisions.reconfirm_roadmap(
            ANALYST, PROJECT, "F1", grant=grant, basis=1, revision=1
        )
    assert await world.column("SELECT count(*) FROM reconfirmations WHERE subject = 'roadmap'") == [
        1
    ]


async def test_the_last_reconfirmation_takes_up_held_checks_whichever_owner_it_is(
    world: DecisionWorld,
) -> None:
    await world.integration.require_check(PROJECT, ZERO_STAYS_ZERO)
    base = await world.awaiting_builder("B1")
    candidate = await world.reviewed("B1", on=base)
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    running = asyncio.create_task(world.integration_with(barrier).integrate(PROJECT, "B1", "once"))
    await held_at_the_barrier(running, barrier)
    question = await world.ask("S1")
    barrier.release.set()
    await running
    await world.answer(question)

    await world.reconfirm(await owed_for(world, "behaviour"))
    assert world.trunk() == base
    await world.reconfirm(await owed_for(world, "story"))  # the Analyst is last this time

    assert world.trunk() == candidate


async def test_a_contract_is_amended_only_once_its_story_stands_and_the_old_red_is_set_aside(
    world: DecisionWorld,
) -> None:
    await world.awaiting_builder("B1")
    await decided(world)
    owed = await owed_for(world, "behaviour")
    grant = await world.claims.claim(VERIFIER, owed["assignment_id"])  # type: ignore[arg-type]
    amended = replace(CONTRACT, contract_id="B1-reduced-rate")

    with pytest.raises(ActionRefused, match="S1 is not current"):
        await world.decisions.amend_contract(
            VERIFIER, PROJECT, "B1", amended, grant=grant, basis=1, replaces=CONTRACT.digest
        )
    with pytest.raises(ActionRefused, match="for the verifier"):
        await world.decisions.reconfirm_contract(
            ANALYST, PROJECT, "B1", grant=grant, basis=1, contract_digest=CONTRACT.digest,
            evidence=await world.decisions.evidence_of(PROJECT, "B1"),
        )  # fmt: skip
    assert (await world.decisions.evidence_of(PROJECT, "B1")).red is not None
    await world.reconfirm(await owed_for(world, "story"))

    await world.decisions.amend_contract(
        VERIFIER, PROJECT, "B1", amended, grant=grant, basis=1, replaces=CONTRACT.digest
    )

    assert (await world.decisions.evidence_of(PROJECT, "B1")).red is None
