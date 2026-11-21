"""What a story demonstrates once one of its behaviours has been replaced. A superseded
behaviour is history: it demonstrates nothing. Sealing, assessing and the Product Owner's
review all use the same view, and a seal made before a replacement is not a seal of what
is in force after it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.integration import Outcome
from relay.application.stories import StoryDraft
from relay.domain.behaviour import BehaviourState
from relay.domain.story import ActionRefused, SealRefused, StoryState
from relay.git.checkout import RemoteUnavailable
from relay.git.mirror import Mirror
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
    # 3AII-S3-II-2.2. Another decision asks nothing more about B1: the declaration
    # stands until the replacement is on trunk, and nobody is invited to contradict it.
    await world.answer(await world.ask("S1"))
    assert [(each["subject"], each["behaviour_id"]) for each in await world.owed()] == [
        ("story", None)
    ]
    await world.reconfirm_everything()
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


async def test_a_seal_written_before_a_replacement_and_sent_after_it_is_refused(
    world: Superseding,
) -> None:
    """3AII-S3-II-1.2. The Verifier read a list in which B1 and Z both showed AC1."""
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.deliver("B1")
    base = await z_awaiting_builder(world, ["AC1"])
    revision, listed = await world.stories.revisions_of(PROJECT, "S1")

    await z_replaces_b1(world, base)

    with pytest.raises(ActionRefused, match="list"):
        await world.stories.seal(
            VERIFIER, PROJECT, "S1",
            story_revision=revision, list_revision=listed, basis=await world.basis("S1"),
        )  # fmt: skip
    assert await world.stories.seal_of(PROJECT, "S1") is None


async def z_pushed_and_unanswered(world: Superseding, monkeypatch: pytest.MonkeyPatch) -> str:
    """B1 and Z in story 1; Z's excused push has gone and nothing came back."""
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.deliver("B1")
    base = await z_awaiting_builder(world, ["AC1"])
    await world.decide(await world.propose())
    candidate = await world.reviewed("Z", {"vat.py": VAT_APPLIES_REDUCED_RATE}, on=base)

    async def unreachable(*_: Any, **__: Any) -> None:
        raise RemoteUnavailable("no route to host")

    monkeypatch.setattr(Mirror, "_push", unreachable)  # until the test undoes it
    assert (await world.integrate("Z")).outcome is Outcome.PENDING
    return candidate


async def test_a_reconfirmation_owed_for_a_behaviour_is_withdrawn_when_it_is_replaced(
    world: Superseding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3AII-S3-II-1.6. The decision came while the replacement's push was unanswered."""
    candidate = await z_pushed_and_unanswered(world, monkeypatch)
    await world.answer(await world.ask("S1"))
    owed = {each["behaviour_id"]: each for each in await world.owed()}
    assert "B1" in owed

    async with world.database.connection() as connection:
        await connection.execute("UPDATE outbox SET published_at = now()")  # all sent once
    monkeypatch.undo()
    await world.integration.reconcile()

    # 3AII-S3-II-2.3: what was withdrawn is not sent again.
    assert await world.column(
        "SELECT count(*) FROM outbox WHERE published_at IS NULL AND "
        f"envelope->'payload'->>'assignment_id' = '{owed['B1']['assignment_id']}'"
    ) == [0]
    assert world.trunk() == candidate
    assert "B1" not in {each["behaviour_id"] for each in await world.owed()}
    with pytest.raises(ActionRefused):
        await world.claims.claim(VERIFIER, owed["B1"]["assignment_id"])


async def test_retiring_a_behaviour_takes_its_row_before_its_storys(
    world: Superseding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3AII-S3-II-1.3. A reconfirmation holds the behaviour and then asks for its story.
    A settlement that held the story and then asked for the behaviour would wait on it
    for ever. With the behaviour held here, the settlement must be waiting for it
    without holding the story."""
    candidate = await z_pushed_and_unanswered(world, monkeypatch)
    monkeypatch.undo()
    async with world.database.connection() as held, held.transaction():
        await held.execute("SELECT 1 FROM behaviours WHERE behaviour_id = 'B1' FOR UPDATE")
        settling = asyncio.create_task(world.integration.reconcile())
        await world.until_someone_waits_for_a_lock()

        await held.execute("SELECT 1 FROM stories WHERE story_id = 'S1' FOR UPDATE NOWAIT")

    await settling
    assert world.trunk() == candidate
    assert (await world.obligation_of("B1")).state == "retired"
