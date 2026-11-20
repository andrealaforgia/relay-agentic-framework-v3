"""Histories found in review of the decisions step: a grant used in another project, a
context read across features, evidence labelled with a contract it was not observed
under, a lock order that could deadlock, publication ahead of reconfirmation, and a
reconfirmation whose follow-up was cut short."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application import behaviours as behaviours_module
from relay.application import stories as stories_module
from relay.application.behaviours import Behaviours
from relay.application.features import Features
from relay.application.reviews import Reviews
from relay.application.stories import StoryDraft
from relay.domain.behaviour import BehaviourState
from relay.domain.story import ActionRefused, StoryState
from relay.testing.oracle import ContainerIsolation, ProcessOracle
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import (
    ANALYST,
    CONTRACT,
    PRODUCT_OWNER,
    VAT_APPLIES_STANDARD_RATE,
    VERIFIER,
    HeldAtABarrier,
    build_grant,
    held_at_the_barrier,
    run_green,
)
from tests.integration.story_world import GREETS, STANDARD_RATE
from tests.integration.trunk_world import PROJECT, ZERO_STAYS_ZERO

REDUCED_RATE = replace(CONTRACT, contract_id="B1-reduced-rate")
ANOTHER = StoryDraft("T1", "Another feature", "something else", {"AC1": "it works"})


@pytest.fixture
async def world(database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> DecisionWorld:
    world = DecisionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    return world


async def decided(world: DecisionWorld, story: str | None = "S1") -> None:
    await world.answer(await world.ask(story))


async def owed_for(world: DecisionWorld, subject: str, story: str = "S1") -> dict[str, Any]:
    [owed] = [
        each
        for each in await world.owed()
        if each["subject"] == subject and each["story_id"] in (story, None)
    ]
    return owed


async def test_a_grant_from_one_project_does_nothing_in_another(
    world: DecisionWorld, database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    (tmp_path / "other").mkdir()
    other = DecisionWorld(database, tmp_path / "other", runner_image, project="zeta")
    await other.open()
    await other.decide(await other.roadmap(STANDARD_RATE))
    await other.awaiting_builder("B1")
    await world.awaiting_builder("B1")
    await decided(world)
    await world.reconfirm(await owed_for(world, "story"))
    owed = await owed_for(world, "behaviour")
    grant = await world.claims.claim(VERIFIER, owed["assignment_id"])

    with pytest.raises(ActionRefused, match="not the current one"):
        await other.decisions.amend_contract(
            VERIFIER, "zeta", "B1", REDUCED_RATE, grant=grant, basis=0, replaces=CONTRACT.digest
        )
    with pytest.raises(ActionRefused, match="not the current one"):
        await other.decisions.reconfirm_contract(
            VERIFIER, "zeta", "B1", grant=grant, basis=0, contract_digest=CONTRACT.digest,
            evidence=await other.decisions.evidence_of("zeta", "B1"),
        )  # fmt: skip

    assert await other.contract_digest("B1") == CONTRACT.digest
    assert await other.state("B1") is BehaviourState.IMPLEMENTING
    assert await other.column(
        "SELECT state FROM assignments WHERE project_id = 'zeta' AND kind = 'build'"
    ) == ["wanted"]
    await world.decisions.amend_contract(
        VERIFIER, PROJECT, "B1", REDUCED_RATE, grant=grant, basis=1, replaces=CONTRACT.digest
    )
    assert await world.contract_digest("B1") == REDUCED_RATE.digest


async def test_the_context_of_a_story_is_asked_for_under_its_own_feature(
    world: DecisionWorld,
) -> None:
    await world.roadmap(ANOTHER, feature="F2")
    await decided(world, story=None)
    assumed = await world.decisions.assume(ANALYST, PROJECT, "F2", text="whole pounds")
    await world.replace(assumed, "pence")

    with pytest.raises(ActionRefused, match="not a story of"):
        await world.decisions.context_for(PROJECT, "F1", "T1")
    with pytest.raises(ActionRefused, match="unknown feature"):
        await world.decisions.context_for(PROJECT, "F9")

    theirs = await world.decisions.context_for(PROJECT, "F2", "T1")
    assert ([each.text for each in theirs.decisions], theirs.basis) == (["pence"], 2)
    ours = await world.decisions.context_for(PROJECT, "F1", "S1")
    assert ([each.text for each in ours.decisions], ours.basis) == (["zero-rated"], 1)


async def test_a_green_is_recorded_under_the_contract_it_was_observed_against(
    world: DecisionWorld,
) -> None:
    base = await world.awaiting_builder("B1")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    build = await build_grant(world.behaviours, PROJECT, "B1")
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    slow = Behaviours(world.database, world.mirrors, ProcessOracle(barrier))
    running = asyncio.create_task(slow.run_green(PROJECT, "B1", 1, build))
    await held_at_the_barrier(running, barrier)
    await decided(world)
    await world.reconfirm(await owed_for(world, "story"))
    grant = await world.claims.claim(
        VERIFIER, (await owed_for(world, "behaviour"))["assignment_id"]
    )
    await world.decisions.amend_contract(
        VERIFIER, PROJECT, "B1", REDUCED_RATE, grant=grant, basis=1, replaces=CONTRACT.digest
    )

    barrier.release.set()
    assert not (await running).is_valid

    assert await world.column("SELECT contract_digest FROM executions WHERE purpose = 'green'") == [
        CONTRACT.digest
    ]
    assert await world.column(
        "SELECT payload->>'contract_digest' FROM events "
        "WHERE event_type = 'behaviour.green_rejected'"
    ) == [CONTRACT.digest]


async def test_an_assumption_and_the_first_roadmap_do_not_deadlock(
    database: AsyncConnectionPool,
    tmp_path: Path,
    runner_image: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = DecisionWorld(database, tmp_path, runner_image)
    await world.open()
    await world.stories.open_feature(PRODUCT_OWNER, PROJECT, "F1", "prices need VAT")
    holding, go_on = asyncio.Event(), asyncio.Event()
    real = stories_module._locked_project

    async def held_with_the_project(*arguments: Any) -> None:
        await real(*arguments)
        if not holding.is_set():
            holding.set()
            await go_on.wait()

    monkeypatch.setattr(stories_module, "_locked_project", held_with_the_project)
    publishing = asyncio.create_task(
        world.stories.publish_roadmap(ANALYST, PROJECT, "F1", [STANDARD_RATE], basis=0)
    )
    await holding.wait()
    assuming = asyncio.create_task(
        world.decisions.assume(ANALYST, PROJECT, "F1", text="amounts are whole pounds")
    )
    await world.until_someone_waits_for_a_lock()
    go_on.set()

    await publishing
    await assuming
    assert (await world.events())[-2:] == ["roadmap.gate_opened", "decision.assumed"]


async def test_a_candidate_is_not_published_until_its_contract_is_reconfirmed(
    world: DecisionWorld,
) -> None:
    base = await world.awaiting_builder("B1")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    assert (await run_green(world.behaviours, PROJECT, "B1", 1)).is_valid
    [ref] = await world.column("SELECT candidate_ref FROM review_cycles")

    await decided(world)  # answering runs the pass, which would publish

    assert await world.column("SELECT publication FROM review_cycles") == ["pending"]
    assert world.git("for-each-ref", str(ref), at=world.remote) == ""
    assert await world.column(
        "SELECT count(*) FROM outbox WHERE envelope->>'event_type' = 'review.requested'"
    ) == [0]
    assert await world.reviews.publish_candidates() == 0
    await world.reconfirm_everything()
    assert await world.column("SELECT publication FROM review_cycles") == ["published"]
    assert await world.column(
        "SELECT count(*) FROM outbox WHERE envelope->>'event_type' = 'review.requested'"
    ) == [2]


async def held_checks_and_an_answer(world: DecisionWorld) -> tuple[str, str]:
    """Checks held by a question, the question answered, the contract reconfirmed: the
    story's reconfirmation is the last thing owed. Returns the base and the candidate."""
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
    return base, candidate


@pytest.mark.parametrize("recovery", ["the same request again", "a restart"])
async def test_a_reconfirmation_whose_follow_up_was_cut_short_is_still_followed_up(
    world: DecisionWorld, monkeypatch: pytest.MonkeyPatch, recovery: str
) -> None:
    base, candidate = await held_checks_and_an_answer(world)
    owed = await owed_for(world, "story")
    grant = await world.claims.claim(ANALYST, owed["assignment_id"])

    async def cut_short(self: Features, project_id: str, **options: Any) -> None:
        raise asyncio.CancelledError

    with monkeypatch.context() as patched:
        patched.setattr(Features, "reconcile", cut_short)
        with pytest.raises(asyncio.CancelledError):
            await world.decisions.reconfirm_story(
                ANALYST, PROJECT, "S1", grant=grant, basis=1, revision=1
            )
    assert world.trunk() == base
    assert await world.owed() == []

    if recovery == "a restart":
        await Features(
            world.database, Reviews(world.database, world.mirrors), world.integration_with()
        ).start()
    else:
        await world.decisions.reconfirm_story(
            ANALYST, PROJECT, "S1", grant=grant, basis=1, revision=1
        )

    assert world.trunk() == candidate
    assert await world.column("SELECT count(*) FROM integration_intents") == [1]
    assert await world.column("SELECT count(*) FROM reconfirmations WHERE subject = 'story'") == [1]


async def test_a_gate_already_superseded_is_stale_whatever_else_is_waiting(
    world: DecisionWorld,
) -> None:
    await world.deliver("B1")
    await world.seal()
    await world.assess("B1 shows it")
    old_gate = await world.gate()
    first, second = await world.ask("S1"), await world.ask("S1")
    await world.answer(first)

    receipt = await world.decide(old_gate)

    assert receipt.disposition == "stale"
    assert len(await world.decisions.open_questions(PROJECT)) == 1
    await world.answer(second)


async def test_a_refusal_names_every_reason_the_closure_first(world: DecisionWorld) -> None:
    await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"], basis=0)
    await world.behaviours.begin_test_design(PROJECT, "B1")
    await world.behaviours.register_acceptance_test(PROJECT, "B1", CONTRACT, basis=0)
    await decided(world)
    await world.reconfirm(await owed_for(world, "story"))
    again = await world.ask("S1")
    owed = await owed_for(world, "behaviour")

    with pytest.raises(ActionRefused) as refused:
        await world.behaviours.run_red(PROJECT, "B1")

    said = str(refused.value)
    assert f"question {again}" in said and "the contract of B1 is not current" in said
    assert said.index("question") < said.index("not current")
    with pytest.raises(ActionRefused, match="question"):
        await world.claims.claim(VERIFIER, owed["assignment_id"])


async def test_a_second_answer_is_told_which_decision_stands(world: DecisionWorld) -> None:
    question = await world.ask("S1")
    await world.answer(question)
    [decision] = (await world.decisions.context_for(PROJECT, "F1", "S1")).decisions

    late = await world.answer(question, "standard rate")

    assert late.disposition == "stale"
    assert str(decision.decision_id) in late.outcome


async def test_a_roadmap_published_just_before_a_question_is_covered_by_it(
    database: AsyncConnectionPool,
    tmp_path: Path,
    runner_image: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = DecisionWorld(database, tmp_path, runner_image)
    await world.open()
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
    asking = asyncio.create_task(world.ask(story=None))
    await world.until_someone_waits_for_a_lock()
    go_on.set()
    gate = await publishing
    await asking

    assert (await world.events())[-2:] == ["roadmap.gate_opened", "question.asked"]
    with pytest.raises(ActionRefused, match="question"):
        await world.decide(gate)


@pytest.mark.parametrize("decision", ["an answer about another story", "a replacement"])
async def test_a_decision_waits_for_work_that_was_admitted_before_it(
    world: DecisionWorld, monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"], basis=0)
    assumed = await world.decisions.assume(ANALYST, PROJECT, "F1", story_id="S1", text="pounds")
    about_the_other_story = await world.ask("S2")
    admitted, go_on = asyncio.Event(), asyncio.Event()
    real = behaviours_module.require_story_at_work

    async def held_once_admitted(*arguments: Any, **options: Any) -> None:
        await real(*arguments, **options)
        admitted.set()
        await go_on.wait()

    monkeypatch.setattr(behaviours_module, "require_story_at_work", held_once_admitted)
    starting = asyncio.create_task(world.behaviours.begin_test_design(PROJECT, "B1"))
    await admitted.wait()
    deciding = asyncio.create_task(
        world.answer(about_the_other_story)
        if decision.startswith("an answer")
        else world.replace(assumed, "pence")
    )
    await world.until_someone_waits_for_a_lock()
    go_on.set()
    await starting
    assert (await deciding).disposition == "effective"

    events = await world.events()
    landed = "question.answered" if decision.startswith("an answer") else "decision.replaced"
    assert events.index("behaviour.test_design_started") < events.index(landed)
    assert await world.story("S1") is StoryState.RELEASED


async def test_a_contract_reconfirmed_or_amended_twice_under_its_grant_is_done_once(
    world: DecisionWorld,
) -> None:
    await world.awaiting_builder("B1")
    await decided(world)
    await world.reconfirm(await owed_for(world, "story"))
    grant = await world.claims.claim(
        VERIFIER, (await owed_for(world, "behaviour"))["assignment_id"]
    )
    kept = await world.decisions.evidence_of(PROJECT, "B1")

    for _ in range(2):
        await world.decisions.reconfirm_contract(
            VERIFIER, PROJECT, "B1", grant=grant, basis=1, contract_digest=CONTRACT.digest,
            evidence=kept,
        )  # fmt: skip

    assert await world.column(
        "SELECT count(*) FROM reconfirmations WHERE subject = 'behaviour'"
    ) == [1]
    with pytest.raises(ActionRefused, match="not the current one"):
        # Reconfirmed, not amended: the grant is spent, and this is not what it did.
        await world.decisions.amend_contract(
            VERIFIER, PROJECT, "B1", REDUCED_RATE, grant=grant, basis=1, replaces=CONTRACT.digest
        )
    await decided(world)
    await world.reconfirm(await owed_for(world, "story"))
    again = await world.claims.claim(
        VERIFIER, (await owed_for(world, "behaviour"))["assignment_id"]
    )

    for _ in range(2):
        await world.decisions.amend_contract(
            VERIFIER, PROJECT, "B1", REDUCED_RATE, grant=again, basis=2, replaces=CONTRACT.digest
        )

    assert (await world.events()).count("behaviour.contract_amended") == 1
    assert await world.column("SELECT red_cycle FROM behaviours") == [1]
