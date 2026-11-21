"""What a story demonstrates once one of its behaviours has been replaced. A superseded
behaviour is history: it demonstrates nothing. Sealing, assessing and the Product Owner's
review all use the same view, and a seal made before a replacement is not a seal of what
is in force after it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.integration import Outcome
from relay.application.stories import StoryDraft
from relay.domain.behaviour import BehaviourState
from relay.domain.story import ActionRefused, SealRefused, StoryState
from tests.integration.fixtures import ANALYST, CONTRACT, VERIFIER
from tests.integration.story_world import GREETS, STANDARD_RATE
from tests.integration.test_supersession import (
    REDUCED_RATE,
    VAT_APPLIES_REDUCED_RATE,
    Superseding,
)
from tests.integration.trunk_world import PROJECT

TWO_CRITERIA = StoryDraft(
    "S1", "Rates", "prices are shown", {"AC1": "the rate is applied", "AC2": "the rate is named"}
)


@pytest.fixture
async def world(database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> Superseding:
    world = Superseding(database, tmp_path, runner_image)
    world.base = await world.open()
    return world


async def z_awaiting_builder(world: Superseding, demonstrates: list[str]) -> str:
    """Z, in story 1 beside B1, with the contract that contradicts it. Returns trunk."""
    await world.behaviours.propose(
        PROJECT, "S1", "Z", "the reduced rate", demonstrates,
        basis=await world.basis("S1"), story_revision=await world.revision("S1"),
    )  # fmt: skip
    await world.behaviours.begin_test_design(PROJECT, "Z")
    await world.behaviours.register_acceptance_test(
        PROJECT, "Z", REDUCED_RATE, basis=await world.basis("S1")
    )
    assert (await world.behaviours.run_red(PROJECT, "Z")).is_valid
    return world.trunk()


async def z_replaces_b1(world: Superseding, base: str) -> None:
    await world.decide(await world.propose())
    await world.reviewed("Z", {"vat.py": VAT_APPLIES_REDUCED_RATE}, on=base)
    assert (await world.integrate("Z")).outcome is Outcome.INTEGRATED


async def test_a_replaced_behaviour_is_shown_as_history_and_its_replacement_carries_the_criterion(
    world: Superseding,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.deliver("B1")
    await z_replaces_b1(world, await z_awaiting_builder(world, ["AC1"]))

    await world.seal()
    await world.assess("Z shows the reduced rate, which is what is wanted now")

    [item] = await world.stories.inbox(PROJECT)
    assert item.manifest["demonstrated_by"] == {"AC1": ["Z"]}
    shown = {
        each["behaviour_id"]: each.get("superseded_by") for each in item.manifest["behaviours"]
    }
    assert shown == {"B1": "Z", "Z": None}
    assert (await world.decide(item.gate_id)).disposition == "effective"
    assert await world.story("S1") is StoryState.APPROVED


async def test_a_criterion_only_the_replaced_behaviour_showed_is_no_longer_shown(
    world: Superseding,
) -> None:
    await world.decide(await world.roadmap(TWO_CRITERIA, GREETS))
    await world.deliver("B1")  # demonstrates AC1
    await z_replaces_b1(world, await z_awaiting_builder(world, ["AC2"]))

    with pytest.raises(SealRefused, match=r"no behaviour demonstrates \['AC1'\]"):
        await world.seal()

    assert await world.story("S1") is StoryState.RELEASED


async def test_a_seal_made_before_a_replacement_is_not_a_seal_of_what_is_in_force_after_it(
    world: Superseding,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.deliver("B1")
    base = await z_awaiting_builder(world, ["AC1"])
    await world.seal()
    await world.assess("B1 and Z between them show the rate")
    assert await world.story("S1") is StoryState.RELEASED  # Z is not on trunk yet

    await z_replaces_b1(world, base)

    assert await world.stories.seal_of(PROJECT, "S1") is None
    assert await world.story("S1") is StoryState.RELEASED
    assert await world.stories.inbox(PROJECT) == []
    await world.seal()
    await world.assess("Z alone shows the rate now")
    assert await world.story("S1") is StoryState.AWAITING_APPROVAL


async def test_a_contract_on_trunk_that_no_longer_stands_is_declared_to_be_replaced(
    world: Superseding,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.deliver("B1")
    await world.answer(await world.ask("S1"))
    owed = {each["subject"]: each for each in await world.owed()}
    await world.reconfirm(owed["story"])
    grant = await world.claims.claim(VERIFIER, owed["behaviour"]["assignment_id"])
    obligation = await world.obligation_of("B1")
    declared: dict[str, Any] = {"grant": grant, "basis": 1, "obligation": obligation.obligation_id}

    with pytest.raises(ActionRefused, match="for the verifier"):
        await world.decisions.declare_replaced(ANALYST, PROJECT, "B1", **declared)
    with pytest.raises(ActionRefused, match="not the obligation"):
        await world.decisions.declare_replaced(
            VERIFIER, PROJECT, "B1", **{**declared, "obligation": grant}
        )
    await world.decisions.declare_replaced(VERIFIER, PROJECT, "B1", **declared)

    assert await world.owed() == []
    assert await world.in_force() == [CONTRACT.contract_id]  # a declaration removes nothing
    with pytest.raises(ActionRefused, match="B1 is to be replaced"):
        await world.seal()
    # Other work in the story goes on meanwhile.
    await z_replaces_b1(world, await z_awaiting_builder(world, ["AC1"]))
    assert await world.state("B1") is BehaviourState.INTEGRATED
    await world.seal()
    await world.assess("Z shows it, zero-rated books aside")
    assert await world.story("S1") is StoryState.AWAITING_APPROVAL


async def test_only_a_behaviour_on_trunk_is_declared_to_be_replaced(world: Superseding) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.awaiting_builder("B1")
    await world.answer(await world.ask("S1"))
    owed = {each["subject"]: each for each in await world.owed()}
    await world.reconfirm(owed["story"])
    grant = await world.claims.claim(VERIFIER, owed["behaviour"]["assignment_id"])

    with pytest.raises(ActionRefused, match="not on trunk"):
        await world.decisions.declare_replaced(
            VERIFIER, PROJECT, "B1", grant=grant, basis=1, obligation=grant
        )

    assert [each["subject"] for each in await world.owed()] == ["behaviour"]


async def test_a_decision_asks_nothing_about_a_behaviour_that_was_replaced(
    world: Superseding,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.deliver("B1")
    await z_replaces_b1(world, await z_awaiting_builder(world, ["AC1"]))

    await world.answer(await world.ask("S1"))

    assert sorted(
        (each["subject"], each["behaviour_id"]) for each in await world.owed() if each["story_id"]
    ) == [("behaviour", "Z"), ("story", None)]
