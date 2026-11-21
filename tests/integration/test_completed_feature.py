"""A completed feature is finished, and its summary says what was delivered. Nothing
changes its context afterwards: the summary is a record of one moment, and what it
records must stay what was true then."""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.integration import Outcome
from relay.application.reviews import NewFinding, Review
from relay.application.stories import Dropped, Kept, StoryDraft
from relay.domain.evidence import Expected, Launch
from relay.domain.review import Carried, Severity
from relay.domain.story import ActionRefused, Receipt
from tests.integration.completion_world import CompletionWorld, last_story_at_its_gate
from tests.integration.fixtures import (
    ANALYST,
    PRODUCT_OWNER,
    VAT_APPLIES_STANDARD_RATE,
    VERIFIER,
)
from tests.integration.story_world import GREETS, INJECTION, STANDARD_RATE, STYLE
from tests.integration.test_supersession import (
    REDUCED_RATE,
    VAT_APPLIES_REDUCED_RATE,
    Superseding,
)
from tests.integration.trunk_world import GREETING, PROJECT

NOT_A_NUMBER = NewFinding(Severity.LOW, "a word is not refused", "vat.py", "say it is not a number")
ROUNDING = NewFinding(Severity.LOW, "rounding is not stated", "vat.py", "state it")


class Replacing(Superseding, CompletionWorld):
    """A project carried to completion in which one behaviour replaces another."""


@pytest.fixture
async def world(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> CompletionWorld:
    world = CompletionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    return world


async def everything(world: CompletionWorld) -> list[Any]:
    """Whatever a late action could have changed."""
    return [
        await world.summary(),
        await world.feature(),
        await world.column("SELECT state FROM assignments ORDER BY created_at"),
        await world.column("SELECT count(*) FROM decisions"),
        await world.column("SELECT count(*) FROM questions"),
        await world.column("SELECT state FROM roadmaps ORDER BY revision"),
        await world.column("SELECT state FROM gates ORDER BY opened_at"),
    ]


async def test_nothing_changes_a_completed_feature(world: CompletionWorld) -> None:
    """S3-15.7."""
    gate = await last_story_at_its_gate(world)
    assumed = await world.decisions.assume(ANALYST, PROJECT, "F1", text="prices are in pounds")
    approval = world.action(gate)
    assert await world.features.decide(approval) == Receipt("effective", "approved")
    assert await world.feature() == "completed"
    before = await everything(world)

    for action in ("pause", "cancel"):
        assert (await world.close(action, reason="too late")).disposition == "stale"
    assert (await world.close("resume")).disposition == "stale"
    with pytest.raises(ActionRefused, match="completed"):
        await world.ask(story=None)
    with pytest.raises(ActionRefused, match="completed"):
        await world.ask("S1")
    with pytest.raises(ActionRefused, match="completed"):
        await world.decisions.assume(VERIFIER, PROJECT, "F1", text="and in euros")
    with pytest.raises(ActionRefused, match="completed"):
        await world.decisions.assume(VERIFIER, PROJECT, "F1", story_id="S1", text="and in euros")
    assert (await world.replace(assumed, "prices are in euros")).disposition == "stale"
    with pytest.raises(ActionRefused, match="completed"):
        await world.revise(Kept("S1"), GREETS)
    # The request that completed it, sent again, is answered as it was.
    assert await world.features.decide(approval) == Receipt("effective", "approved")
    await world.features.start()

    assert await everything(world) == before
    assert await world.batches() == ["completed"]


async def test_the_summary_tells_the_decisions_of_a_person_from_those_of_a_model(
    world: CompletionWorld,
) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE))
    answered = await world.ask(story=None)
    await world.answer(answered, "the standard rate")
    await world.reconfirm_everything()
    guessed = await world.decisions.assume(ANALYST, PROJECT, "F1", text="prices are in pounds")
    await world.decisions.assume(ANALYST, PROJECT, "F1", text="amounts are rounded down")
    await world.replace(guessed, "prices are in euros")  # no longer in force, so not listed
    await world.reconfirm_everything()
    await world.deliver("B1")
    await world.seal()
    await world.assess("B1 shows 120 for 100")

    await world.decide(await world.gate())

    summary = await world.summary()
    assert summary is not None
    assert [(each["source"], each["text"]) for each in summary["decisions"]] == [
        ("product_owner", "the standard rate"),
        ("model", "amounts are rounded down"),
        ("product_owner", "prices are in euros"),
    ]
    assert [each["text"] for each in summary["assumptions"]] == ["amounts are rounded down"]


async def test_the_summary_lists_what_was_left_open_and_not_what_was_fixed(
    world: CompletionWorld,
) -> None:
    """S3-15.8. A finding verified fixed on the delivered candidate can still read
    `open`; it is not a limitation of what was delivered."""
    await world.decide(await world.roadmap(STANDARD_RATE))
    base = await world.awaiting_builder("B1")
    await world.reviewed(
        "B1",
        on=base,
        reviews={
            "sentinel": Review(findings=(INJECTION,)),
            "qa": Review(findings=(NOT_A_NUMBER, ROUNDING)),
        },
    )

    async def raised(finding: NewFinding) -> str:
        [found] = await world.column(
            f"SELECT finding_id FROM findings WHERE claim = '{finding.claim}'"
        )
        return str(found)

    await world.reviewed(
        "B1",
        {"vat.py": VAT_APPLIES_STANDARD_RATE + "# parsed as an integer\n"},
        on=base,
        reviews={
            "sentinel": Review(carried={await raised(INJECTION): Carried.VERIFIED_FIXED}),
            # Of three small things, one was fixed, one was taken back, one is left.
            "qa": Review(
                findings=(STYLE,),
                carried={
                    await raised(NOT_A_NUMBER): Carried.VERIFIED_FIXED,
                    await raised(ROUNDING): Carried.WITHDRAWN,
                },
                withdrawal_reasons={await raised(ROUNDING): "the story says how"},
            ),
        },
    )
    assert (await world.integrate("B1")).outcome is Outcome.INTEGRATED
    await world.seal()
    await world.assess("B1 shows 120 for 100")

    await world.decide(await world.gate())

    summary = await world.summary()
    assert summary is not None
    [limitation] = summary["limitations"]
    assert (limitation["behaviour_id"], limitation["role"], limitation["claim"]) == (
        "B1",
        "qa",
        STYLE.claim,
    )
    assert limitation["finding_id"] == await raised(STYLE)


async def test_the_summary_names_the_roadmap_revisions_there_were(world: CompletionWorld) -> None:
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    await world.decide(
        await world.revise(Kept("S1"), Dropped("S2", story_revision=1, story_basis=0))
    )
    await world.deliver("B1")
    await world.seal()
    await world.assess("B1 shows 120 for 100")

    await world.decide(await world.gate())

    summary = await world.summary()
    assert summary is not None
    assert (summary["roadmap_revision"], summary["earlier_revisions"]) == (2, [1])
    assert [(s["story_id"], s["outcome"]) for s in summary["stories"]] == [
        ("S1", "accepted"),
        ("S2", "withdrawn"),
    ]


async def test_the_change_owed_after_a_failed_check_is_not_something_a_worker_claims(
    world: CompletionWorld,
) -> None:
    gate = await last_story_at_its_gate(world)
    await world.integration.require_check(PROJECT, GREETING.as_check())
    await world.decide(gate)
    [failed] = await world.stories.inbox(PROJECT)
    await world.decide(failed.gate_id, "request_changes", reason="greet the shopper")
    [owed] = await world.column("SELECT assignment_id FROM assignments WHERE kind = 'revise'")
    assert isinstance(owed, uuid.UUID)

    with pytest.raises(ActionRefused, match="proposing the roadmap again"):
        await world.claims.claim(ANALYST, owed)


async def test_what_a_replaced_behaviour_left_open_is_not_a_limitation_of_what_was_delivered(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    world = Replacing(database, tmp_path, runner_image)
    world.base = await world.open()
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    base = await world.awaiting_builder("B1")
    await world.reviewed("B1", on=base, reviews={"qa": Review(findings=(STYLE,))})
    assert (await world.integrate("B1")).outcome is Outcome.INTEGRATED
    await world.seal()
    await world.assess("B1 shows 120 for 100")
    await world.decide(await world.gate())
    base = await world.awaiting_builder("Z", REDUCED_RATE, "S2")
    await world.reviewed("Z", {"vat.py": VAT_APPLIES_REDUCED_RATE}, on=base)
    await world.decide(await world.propose())
    assert (await world.integrate("Z")).outcome is Outcome.INTEGRATED
    await world.seal("S2")
    await world.assess("Z shows 105 for 100", "S2")

    await world.decide(await world.gate())

    summary = await world.summary()
    assert summary is not None
    assert [(each["behaviour_id"], each["in_force"]) for each in summary["behaviours"]] == [
        ("B1", False),
        ("Z", True),
    ]
    assert summary["limitations"] == []
    assert [each["behaviour_id"] for each in summary["checked"]] == ["Z"]


FAREWELL = replace(
    GREETING,
    contract_id="B3-farewell",
    observes="the farewell the product prints",
    launch=Launch(
        argv=("sh", "-c", "cat farewell.txt 2>/dev/null || echo none"), timeout_seconds=30
    ),
    expect=Expected(exit_code=0, stdout="bye\n"),
)


def correcting(story: str) -> StoryDraft:
    return StoryDraft(story, "Standard rate, shown properly", "shown with its currency",
                      {"AC1": "100 net is shown as 120"}, corrects="S1")  # fmt: skip


async def test_a_story_is_corrected_by_the_correction_that_was_delivered(
    world: CompletionWorld,
) -> None:
    """3AII-S4-1.5. A correction proposed and then dropped corrected nothing."""
    await world.decide(await world.roadmap(STANDARD_RATE))
    await world.stories.open_feature(PRODUCT_OWNER, PROJECT, "F2", "shoppers want a greeting")
    await world.stories.decide(
        world.action(await world.stories.publish_roadmap(ANALYST, PROJECT, "F2", [GREETS], basis=0))
    )
    await world.deliver("B1")
    await world.seal()
    await world.assess("B1 shows 120 for 100")
    await world.decide(await world.gate(), "reject", reason="the currency is missing")
    # F2 has the project's place, so a correction of S1 stays planned and can be dropped.
    await world.decide(await world.revise(Kept("S1"), correcting("C1")))
    revision, _ = await world.stories.revisions_of(PROJECT, "C1")
    await world.decide(
        await world.revise(
            Kept("S1"),
            Dropped("C1", story_revision=revision, story_basis=await world.basis("C1")),
            correcting("D1"),
        )
    )
    await world.deliver("G", "S2", GREETING, {"greeting.txt": "hello\n"})
    await world.seal("S2")
    await world.assess("G prints hello", "S2")
    await world.decide(await world.gate())
    assert await world.feature("F2") == "completed"
    await world.deliver("D", "D1", FAREWELL, {"farewell.txt": "bye\n"})
    await world.seal("D1")
    await world.assess("D prints bye", "D1")

    await world.decide(await world.gate())

    summary = await world.summary()
    assert summary is not None
    assert [(s["story_id"], s["outcome"], s["corrected_by"]) for s in summary["stories"]] == [
        ("S1", "rejected", "D1"),
        ("C1", "withdrawn", None),
        ("D1", "accepted", None),
    ]
