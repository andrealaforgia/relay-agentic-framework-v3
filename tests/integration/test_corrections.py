"""Changing what a decision has overtaken: a contract whose behaviour is already in
review or parked after a refused push, and a roadmap with a proposal still waiting."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.integration import Outcome
from relay.application.reviews import Review
from relay.application.stories import Kept, Reworded, StoryDraft
from relay.domain.behaviour import BehaviourState
from relay.domain.evidence import Expected
from relay.domain.story import ActionRefused, StoryState
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import (
    ANALYST,
    CONTRACT,
    VAT_APPLIES_STANDARD_RATE,
    VERIFIER,
    prepare_review,
    run_green,
    submit_review,
)
from tests.integration.story_world import GREETS, INJECTION, STANDARD_RATE
from tests.integration.trunk_world import PROJECT

REDUCED_RATE = replace(
    CONTRACT, contract_id="B1-reduced-rate", expect=Expected(exit_code=0, stdout="105\n")
)
VAT_APPLIES_REDUCED_RATE = "import sys\nprint(int(sys.argv[1]) * 105 // 100)\n"
REFUNDS = StoryDraft("S3", "Refunds", "a refund is shown", {"AC1": "a refund of 120 shows 120"})


@pytest.fixture
async def world(database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> DecisionWorld:
    world = DecisionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    return world


async def decided(world: DecisionWorld, story: str | None = "S1") -> None:
    await world.answer(await world.ask(story))


async def owed_for(world: DecisionWorld, subject: str, story: str | None = "S1") -> dict[str, Any]:
    [owed] = [
        each
        for each in await world.owed()
        if each["subject"] == subject
        and each["story_id"] == (story if subject != "roadmap" else None)
    ]
    return owed


async def verifier_grant(world: DecisionWorld) -> Any:
    """The story reconfirmed, and the Verifier holding the reconfirmation of B1's contract."""
    await world.reconfirm(await owed_for(world, "story"))
    return await world.claims.claim(VERIFIER, (await owed_for(world, "behaviour"))["assignment_id"])


async def amend(world: DecisionWorld, grant: Any, contract: Any = REDUCED_RATE) -> None:
    await world.decisions.amend_contract(
        VERIFIER, PROJECT, "B1", contract, grant=grant, basis=1, replaces=CONTRACT.digest
    )


async def test_amending_a_contract_in_review_sets_the_review_aside_and_keeps_its_findings(
    world: DecisionWorld,
) -> None:
    base = await world.awaiting_builder("B1")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    assert (await run_green(world.behaviours, PROJECT, "B1", 1)).is_valid
    assert await world.reviews.publish_candidates() == 1
    async with world.database.connection() as connection:
        tasks = await (
            await connection.execute("SELECT role, capability FROM review_tasks")
        ).fetchall()
    capability = {task["role"]: task["capability"] for task in tasks}
    await prepare_review(world.reviews, capability["sentinel"], world.tmp_path / "sentinel")
    await submit_review(world.reviews, capability["sentinel"], Review(findings=(INJECTION,)))
    await prepare_review(world.reviews, capability["qa"], world.tmp_path / "qa")
    await decided(world)

    await amend(world, await verifier_grant(world))

    assert await world.state("B1") is BehaviourState.VALIDATING_RED
    assert await world.contract_digest("B1") == REDUCED_RATE.digest
    assert await world.column("SELECT state FROM review_cycles") == ["superseded"]
    late = await submit_review(world.reviews, capability["qa"], Review())
    assert late.disposition == "stale"
    assert await world.column("SELECT status FROM findings") == ["open"]

    assert (await world.behaviours.run_red(PROJECT, "B1")).is_valid
    await world.reviewed(
        "B1", {"vat.py": VAT_APPLIES_REDUCED_RATE}, on=base, reviews={"sentinel": Review()}
    )
    assert await world.column("SELECT status FROM findings") == ["open"]
    assert await world.state("B1") is BehaviourState.IMPLEMENTING  # sent back: still blocked
    assert await world.column("SELECT state FROM review_cycles ORDER BY created_at") == [
        "superseded",
        "changes_requested",
    ]


async def test_an_amendment_changes_the_contract_or_it_is_not_one(world: DecisionWorld) -> None:
    await world.awaiting_builder("B1")
    await decided(world)
    grant = await verifier_grant(world)

    with pytest.raises(ActionRefused, match="the same contract"):
        await amend(world, grant, CONTRACT)

    assert await world.state("B1") is BehaviourState.IMPLEMENTING
    assert await world.column("SELECT state FROM assignments WHERE kind = 'build'") == ["wanted"]
    await amend(world, grant)
    assert await world.state("B1") is BehaviourState.VALIDATING_RED


async def test_a_candidate_the_repository_refused_is_not_sent_round_again_by_an_amendment(
    world: DecisionWorld,
) -> None:
    base = await world.awaiting_builder("B1")
    await world.reviewed("B1", on=base)
    world.hook_flag("refuse").touch()
    assert (await world.integrate("B1")).outcome is Outcome.REJECTED
    world.hook_flag("refuse").unlink()
    assert await world.state("B1") is BehaviourState.BLOCKED
    await decided(world)
    grant = await verifier_grant(world)

    with pytest.raises(ActionRefused, match="the same contract"):
        await amend(world, grant, CONTRACT)

    assert await world.state("B1") is BehaviourState.BLOCKED
    assert await world.column("SELECT state FROM integration_intents") == ["rejected"]
    assert await world.column("SELECT state FROM review_cycles") == ["satisfied"]
    assert world.trunk() == base

    await amend(world, grant)

    assert await world.state("B1") is BehaviourState.VALIDATING_RED
    assert await world.column("SELECT state FROM review_cycles") == ["superseded"]
    assert await world.column("SELECT state FROM integration_intents") == ["rejected"]
    assert (await world.behaviours.run_red(PROJECT, "B1")).is_valid


async def test_a_proposal_that_edits_a_story_cannot_be_reconfirmed_once_that_storys_basis_moves(
    world: DecisionWorld,
) -> None:
    reworded = Reworded(
        "S2", "A warmer greeting", "greeted by name", {"AC1": "hello, Ada is printed"},
        story_revision=1, story_basis=0,
    )  # fmt: skip
    old_gate = await world.revise(Kept("S1"), reworded)

    await decided(world, story=None)

    assert (await world.decide(old_gate)).disposition == "stale"
    roadmap = await world.stories.roadmap_of(PROJECT, "F1")
    assert (roadmap.effective, roadmap.proposed) == (1, 2)
    assert await world.column("SELECT title FROM stories WHERE story_id = 'S2'") == ["Greeting"]
    owed = [(each["subject"], each["story_id"], each["revision"]) for each in await world.owed()]
    assert owed == [
        ("roadmap", None, 1),
        ("roadmap", None, 2),
        ("story", "S1", None),
        ("story", "S2", None),
    ]
    proposal = next(each for each in await world.owed() if each["revision"] == 2)
    grant = await world.claims.claim(ANALYST, proposal["assignment_id"])
    with pytest.raises(ActionRefused, match="has to be replaced"):
        await world.decisions.reconfirm_roadmap(
            ANALYST, PROJECT, "F1", grant=grant, basis=1, revision=2
        )
    with pytest.raises(ActionRefused, match="proposal 2 is waiting"):
        await world.stories.publish_roadmap(
            ANALYST, PROJECT, "F1", [Kept("S1"), Kept("S2")], basis=1, revises=1, replaces=None
        )

    again = await world.revise(Kept("S1"), replace(reworded, story_basis=1))

    assert [each["revision"] for each in await world.owed() if each["subject"] == "roadmap"] == [1]
    await world.decide(again)
    assert await world.column("SELECT title FROM stories WHERE story_id = 'S2'") == [
        "A warmer greeting"
    ]
    assert [(each["subject"], each["story_id"]) for each in await world.owed()] == [("story", "S1")]


async def test_a_proposal_that_only_adds_is_reconfirmed_and_put_to_the_person_again(
    world: DecisionWorld,
) -> None:
    await world.revise(Kept("S1"), Kept("S2"), REFUNDS)
    await decided(world, story=None)
    proposal = next(each for each in await world.owed() if each["revision"] == 2)
    grant = await world.claims.claim(ANALYST, proposal["assignment_id"])
    with pytest.raises(ActionRefused, match="not the current one"):
        # The grant is for the proposal. It says nothing about the roadmap in force.
        await world.decisions.reconfirm_roadmap(
            ANALYST, PROJECT, "F1", grant=grant, basis=1, revision=1
        )
    with pytest.raises(ActionRefused, match="is not F1's roadmap"):
        await world.decisions.reconfirm_roadmap(
            ANALYST, PROJECT, "F1", grant=grant, basis=1, revision=7
        )

    await world.decisions.reconfirm_roadmap(
        ANALYST, PROJECT, "F1", grant=grant, basis=1, revision=2
    )

    [item] = await world.stories.inbox(PROJECT)
    assert item.manifest["roadmap_revision"] == 2
    assert [each["text"] for each in item.manifest["decisions"]] == ["zero-rated"]
    await world.decide(item.gate_id)
    assert await world.story("S3") is StoryState.PLANNED
    assert (await world.stories.roadmap_of(PROJECT, "F1")).effective == 2
    # The revision it replaced needs no reconfirming now: the approved one is the answer.
    assert [each["subject"] for each in await world.owed()] == ["story", "story"]


async def test_sending_a_proposal_back_leaves_what_is_owed_for_the_roadmap_in_force(
    world: DecisionWorld,
) -> None:
    await world.revise(Kept("S1"), Kept("S2"), REFUNDS)
    await decided(world, story=None)
    proposal = next(each for each in await world.owed() if each["revision"] == 2)
    grant = await world.claims.claim(ANALYST, proposal["assignment_id"])
    await world.decisions.reconfirm_roadmap(
        ANALYST, PROJECT, "F1", grant=grant, basis=1, revision=2
    )

    await world.decide(await world.gate(), "request_changes", reason="not refunds, not yet")

    assert [(each["subject"], each["revision"]) for each in await world.owed()] == [
        ("roadmap", 1),
        ("story", None),
        ("story", None),
    ]
    assert await world.reconfirm_everything() == 3
    await world.behaviours.propose(
        PROJECT, "S1", "B1", "standard rate", ["AC1"], basis=1, story_revision=1
    )


async def test_an_approved_revision_is_the_answer_for_the_stories_it_rewords(
    world: DecisionWorld,
) -> None:
    await decided(world, "S2")
    assert [(each["subject"], each["story_id"]) for each in await world.owed()] == [("story", "S2")]
    reworded = Reworded(
        "S2", "A greeting for zero-rated books", "greeted", {"AC1": "hello is printed"},
        story_revision=1, story_basis=1,
    )  # fmt: skip

    await world.decide(await world.revise(Kept("S1"), reworded))

    assert await world.owed() == []
    assert await world.column("SELECT current_on FROM stories WHERE story_id = 'S2'") == [1]


REWORDED_S2 = Reworded(
    "S2", "A warmer greeting", "greeted by name", {"AC1": "hello, Ada is printed"},
    story_revision=1, story_basis=0,
)  # fmt: skip


async def test_a_decision_about_a_story_overtakes_a_proposal_that_rewords_it(
    world: DecisionWorld,
) -> None:
    gate = await world.revise(Kept("S1"), REWORDED_S2)

    await decided(world, "S2")

    assert (await world.decide(gate)).disposition == "stale"
    assert await world.column("SELECT title FROM stories WHERE story_id = 'S2'") == ["Greeting"]
    assert [(each["subject"], each["story_id"]) for each in await world.owed()] == [("story", "S2")]


async def test_a_decision_about_another_story_leaves_a_proposal_approvable(
    world: DecisionWorld,
) -> None:
    gate = await world.revise(Kept("S1"), REWORDED_S2)

    await decided(world, "S1")

    assert (await world.decide(gate)).disposition == "effective"
    assert await world.column("SELECT title FROM stories WHERE story_id = 'S2'") == [
        "A warmer greeting"
    ]


async def test_a_story_is_not_reworded_while_a_question_is_open_over_it(
    world: DecisionWorld,
) -> None:
    gate = await world.revise(Kept("S1"), REWORDED_S2)
    question = await world.ask("S2")

    assert (await world.decide(gate)).disposition == "stale"

    assert await world.column("SELECT title FROM stories WHERE story_id = 'S2'") == ["Greeting"]
    with pytest.raises(ActionRefused, match="question"):
        await world.revise(Kept("S1"), REWORDED_S2)
    await world.answer(question)
