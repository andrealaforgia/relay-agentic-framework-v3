"""What a decision of the Product Owner's does to work made before it. Everything in its
scope was made under other decisions: each owner reconfirms or changes what is theirs, a
result that was already on its way is kept as evidence and decides nothing, and what a
person was about to approve is put to them again."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application import decisions as decisions_module
from relay.application.behaviours import Behaviours
from relay.application.decisions import Evidence
from relay.application.integration import Outcome
from relay.application.reviews import Review, SubmissionRefused
from relay.domain.behaviour import BehaviourState
from relay.domain.evidence import Expected, GreenRejection, RedRejection
from relay.domain.story import ActionRefused, Principal, PrincipalKind, StoryState
from relay.git.checkout import RemoteUnavailable
from relay.git.mirror import Mirror
from relay.testing.oracle import ContainerIsolation, ProcessOracle
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import (
    ANALYST,
    CONTRACT,
    VAT_APPLIES_STANDARD_RATE,
    VERIFIER,
    CountingLaunches,
    HeldAtABarrier,
    build_grant,
    held_at_the_barrier,
    run_green,
)
from tests.integration.story_world import GREETS, STANDARD_RATE
from tests.integration.trunk_world import PROJECT, ZERO_STAYS_ZERO

REDUCED_RATE = replace(
    CONTRACT, contract_id="B1-reduced-rate", expect=Expected(exit_code=0, stdout="105\n")
)
ANOTHER_VERIFIER = Principal(PrincipalKind.WORKER, "worker-verifier-2", role="verifier")


@pytest.fixture
async def world(database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> DecisionWorld:
    world = DecisionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    return world


async def decided(world: DecisionWorld, story: str | None = "S1") -> None:
    """A question about the story, asked and answered: its basis moves by one."""
    await world.answer(await world.ask(story))


async def owed_for(world: DecisionWorld, subject: str) -> dict[str, Any]:
    [owed] = [each for each in await world.owed() if each["subject"] == subject]
    return owed


def held(world: DecisionWorld) -> tuple[HeldAtABarrier, Behaviours]:
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    return barrier, Behaviours(world.database, world.mirrors, ProcessOracle(barrier))


async def registered(world: DecisionWorld) -> None:
    await world.behaviours.propose(PROJECT, "S1", "B1", "standard rate", ["AC1"], basis=0)
    await world.behaviours.begin_test_design(PROJECT, "B1")
    await world.behaviours.register_acceptance_test(PROJECT, "B1", CONTRACT, basis=0)


async def test_a_decision_owes_each_owner_a_reconfirmation_and_a_later_one_replaces_them(
    world: DecisionWorld,
) -> None:
    await registered(world)

    await decided(world, story=None)

    first = await world.owed()
    assert [(each["subject"], each["role"], each["basis"]) for each in first] == [
        ("roadmap", "analyst", 1),
        ("story", "analyst", 1),
        ("behaviour", "verifier", 1),
        ("story", "analyst", 1),
    ]
    late = await world.claims.claim(ANALYST, first[1]["assignment_id"])
    [decision] = (await world.decisions.context_for(PROJECT, "F1")).decisions
    await world.replace(decision.decision_id, "standard rate after all")

    second = await world.owed()
    assert [each["basis"] for each in second] == [2, 2, 2, 2]
    assert {each["assignment_id"] for each in first}.isdisjoint(
        each["assignment_id"] for each in second
    )
    with pytest.raises(ActionRefused, match="written on decision basis 1"):
        await world.decisions.reconfirm_story(
            ANALYST, PROJECT, "S1", grant=late, basis=1, revision=1
        )
    with pytest.raises(ActionRefused, match="not the current one"):
        await world.decisions.reconfirm_story(
            ANALYST, PROJECT, "S1", grant=late, basis=2, revision=1
        )
    with pytest.raises(ActionRefused, match="no longer wanted"):
        await world.claims.claim(ANALYST, first[1]["assignment_id"])
    assert await world.reconfirm_everything() == 4
    assert await world.owed() == []
    assert (await world.behaviours.run_red(PROJECT, "B1")).is_valid


async def test_reconfirming_is_the_owners_and_is_about_what_stands_now(
    world: DecisionWorld,
) -> None:
    await registered(world)
    await decided(world)
    story, contract = await owed_for(world, "story"), await owed_for(world, "behaviour")

    with pytest.raises(ActionRefused, match="for a analyst"):
        await world.claims.claim(VERIFIER, story["assignment_id"])
    grant = await world.claims.claim(ANALYST, story["assignment_id"])
    with pytest.raises(ActionRefused, match="for the analyst"):
        await world.decisions.reconfirm_story(
            VERIFIER, PROJECT, "S1", grant=grant, basis=1, revision=1
        )
    with pytest.raises(ActionRefused, match="not S1 as it stands"):
        await world.decisions.reconfirm_story(
            ANALYST, PROJECT, "S1", grant=grant, basis=1, revision=7
        )
    await world.decisions.reconfirm_story(ANALYST, PROJECT, "S1", grant=grant, basis=1, revision=1)

    theirs = await world.claims.claim(VERIFIER, contract["assignment_id"])
    digest = await world.contract_digest("B1")
    with pytest.raises(ActionRefused, match="not the contract"):
        await world.decisions.reconfirm_contract(
            VERIFIER, PROJECT, "B1", grant=theirs, basis=1, contract_digest="sha256:other",
            evidence=Evidence(),
        )  # fmt: skip
    with pytest.raises(ActionRefused, match="not the evidence"):
        await world.decisions.reconfirm_contract(
            VERIFIER, PROJECT, "B1", grant=theirs, basis=1, contract_digest=digest,
            evidence=Evidence(red=str(uuid.uuid7())),
        )  # fmt: skip
    with pytest.raises(ActionRefused, match="something else"):  # the Analyst's, spent on the story
        await world.decisions.reconfirm_contract(
            VERIFIER, PROJECT, "B1", grant=grant, basis=1, contract_digest=digest,
            evidence=Evidence(),
        )  # fmt: skip
    await world.decisions.reconfirm_contract(
        VERIFIER, PROJECT, "B1", grant=theirs, basis=1, contract_digest=digest, evidence=Evidence()
    )

    assert await world.column("SELECT subject || ':' || basis FROM reconfirmations") == [
        "story:1",
        "behaviour:1",
    ]


async def test_red_is_not_run_on_a_contract_nobody_has_reconfirmed(world: DecisionWorld) -> None:
    await registered(world)
    await decided(world)
    await world.reconfirm(await owed_for(world, "story"))
    counting = CountingLaunches(ContainerIsolation(world.image))
    behaviours = Behaviours(world.database, world.mirrors, ProcessOracle(counting))

    with pytest.raises(ActionRefused, match="the contract of B1 is not current"):
        await behaviours.run_red(PROJECT, "B1")

    assert counting.launches == 0
    await world.reconfirm(await owed_for(world, "behaviour"))
    assert (await behaviours.run_red(PROJECT, "B1")).is_valid


async def test_a_red_overtaken_by_a_decision_and_an_amended_contract_is_evidence_only(
    world: DecisionWorld,
) -> None:
    await registered(world)
    barrier, behaviours = held(world)
    running = asyncio.create_task(behaviours.run_red(PROJECT, "B1"))
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

    assert (await running).rejection is RedRejection.SUPERSEDED
    assert await world.state("B1") is BehaviourState.VALIDATING_RED
    assert await world.contract_digest("B1") == REDUCED_RATE.digest
    assert await world.owed() == []
    assert (await world.behaviours.run_red(PROJECT, "B1")).is_valid
    assert await world.column("SELECT count(*) FROM executions WHERE purpose = 'red'") == [2]


async def test_reopening_a_red_does_not_remove_the_reconfirmation_it_would_need(
    world: DecisionWorld,
) -> None:
    await world.awaiting_builder("B1")
    await decided(world)
    await world.reconfirm(await owed_for(world, "story"))

    with pytest.raises(ActionRefused, match="the contract of B1 is not current"):
        await world.behaviours.reopen_red(PROJECT, "B1", "the answer changes the amount")

    owed = await owed_for(world, "behaviour")
    grant = await world.claims.claim(VERIFIER, owed["assignment_id"])
    await world.decisions.amend_contract(
        VERIFIER, PROJECT, "B1", REDUCED_RATE, grant=grant, basis=1, replaces=CONTRACT.digest
    )
    assert await world.state("B1") is BehaviourState.VALIDATING_RED
    assert await world.column(
        "SELECT kind || ':' || state FROM assignments ORDER BY created_at"
    ) == ["build:retired", "reconfirm:done", "reconfirm:done"]
    assert "behaviour.contract_amended" in await world.events()


async def test_an_amendment_under_a_grant_that_was_taken_back_changes_nothing(
    world: DecisionWorld,
) -> None:
    await world.awaiting_builder("B1")
    await decided(world)
    await world.reconfirm(await owed_for(world, "story"))
    owed = await owed_for(world, "behaviour")
    first = await world.claims.claim(VERIFIER, owed["assignment_id"])
    await world.close("pause", reason="one moment")
    await world.close("resume")
    second = await world.claims.claim(ANOTHER_VERIFIER, owed["assignment_id"])

    with pytest.raises(ActionRefused, match="not the current one"):
        await world.decisions.amend_contract(
            VERIFIER, PROJECT, "B1", REDUCED_RATE, grant=first, basis=1, replaces=CONTRACT.digest
        )

    assert await world.contract_digest("B1") == CONTRACT.digest
    assert await world.state("B1") is BehaviourState.IMPLEMENTING
    assert await world.claims.claim(ANOTHER_VERIFIER, owed["assignment_id"]) == second
    with pytest.raises(ActionRefused, match="not the contract"):
        await world.decisions.amend_contract(
            ANOTHER_VERIFIER, PROJECT, "B1", REDUCED_RATE, grant=second, basis=1,
            replaces=REDUCED_RATE.digest,
        )  # fmt: skip
    with pytest.raises(ActionRefused, match="for the verifier"):
        await world.decisions.amend_contract(
            ANALYST, PROJECT, "B1", REDUCED_RATE, grant=second, basis=1, replaces=CONTRACT.digest
        )


async def test_a_contract_on_trunk_cannot_be_amended_only_reconfirmed(
    world: DecisionWorld,
) -> None:
    await world.deliver("B1")
    await decided(world)
    await world.reconfirm(await owed_for(world, "story"))
    owed = await owed_for(world, "behaviour")
    grant = await world.claims.claim(VERIFIER, owed["assignment_id"])

    with pytest.raises(ActionRefused, match="needs a correction"):
        await world.decisions.amend_contract(
            VERIFIER, PROJECT, "B1", REDUCED_RATE, grant=grant, basis=1, replaces=CONTRACT.digest
        )

    assert await world.state("B1") is BehaviourState.INTEGRATED
    kept = await world.decisions.evidence_of(PROJECT, "B1")
    assert kept.red and kept.green and kept.cycle and kept.integration
    await world.decisions.reconfirm_contract(
        VERIFIER, PROJECT, "B1", grant=grant, basis=1, contract_digest=CONTRACT.digest,
        evidence=kept,
    )  # fmt: skip
    assert await world.owed() == []


async def test_a_green_that_was_in_flight_when_the_decision_landed_is_evidence_only(
    world: DecisionWorld,
) -> None:
    base = await world.awaiting_builder("B1")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    grant = await build_grant(world.behaviours, PROJECT, "B1")
    barrier, behaviours = held(world)
    running = asyncio.create_task(behaviours.run_green(PROJECT, "B1", 1, grant))
    await held_at_the_barrier(running, barrier)
    await decided(world)
    assert await world.reconfirm_everything() == 2  # the story, and the contract with its red

    barrier.release.set()

    assert (await running).rejection is GreenRejection.BASIS_SUPERSEDED
    assert await world.state("B1") is BehaviourState.IMPLEMENTING
    assert await world.column("SELECT count(*) FROM review_cycles") == [0]
    assert await world.column("SELECT count(*) FROM executions WHERE purpose = 'green'") == [1]
    assert (await world.behaviours.run_green(PROJECT, "B1", 1, grant)).is_valid
    assert await world.column("SELECT count(*) FROM review_cycles") == [1]


async def test_a_review_written_from_a_tree_handed_over_before_the_decision_is_not_accepted(
    world: DecisionWorld,
) -> None:
    base = await world.awaiting_builder("B1")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    assert (await run_green(world.behaviours, PROJECT, "B1", 1)).is_valid
    assert await world.reviews.publish_candidates() == 1
    async with world.database.connection() as connection:
        task = await (
            await connection.execute(
                "SELECT capability, assignment_id FROM review_tasks WHERE role = 'qa'"
            )
        ).fetchone()
    assert task is not None
    capability = task["capability"]
    qa = Principal(PrincipalKind.WORKER, "worker-qa", role="qa")
    grant = await world.claims.claim(qa, task["assignment_id"])
    before = await world.reviews.prepare_review(capability, world.tmp_path / "before", grant)
    await decided(world)

    with pytest.raises(ActionRefused, match="not current"):
        await world.reviews.prepare_review(capability, world.tmp_path / "too-soon", grant)
    assert not (world.tmp_path / "too-soon").exists()  # refused before any tree is fetched
    await world.reconfirm_everything()
    after = await world.reviews.prepare_review(capability, world.tmp_path / "after", grant)

    with pytest.raises(SubmissionRefused, match="no tree was prepared"):
        await world.reviews.submit_review(capability, Review(), grant, before.receipt)
    accepted = await world.reviews.submit_review(capability, Review(), grant, after.receipt)
    assert accepted.disposition == "effective"
    assert (
        await world.reviews.submit_review(capability, Review(), grant, before.receipt) == accepted
    )


async def test_checks_overtaken_by_a_decision_authorise_nothing_and_are_run_again(
    world: DecisionWorld,
) -> None:
    await world.integration.require_check(PROJECT, ZERO_STAYS_ZERO)
    base = await world.awaiting_builder("B1")
    candidate = await world.reviewed("B1", on=base)
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    running = asyncio.create_task(world.integration_with(barrier).integrate(PROJECT, "B1", "once"))
    await held_at_the_barrier(running, barrier)
    assumed = await world.decisions.assume(ANALYST, PROJECT, "F1", story_id="S1", text="pounds")
    await world.replace(assumed, "pence")
    await world.reconfirm_everything()

    barrier.release.set()
    overtaken = await running

    assert overtaken.outcome is Outcome.SUPERSEDED
    assert "decision" in overtaken.reason
    assert world.trunk() == base
    assert await world.column("SELECT count(*) FROM integration_intents") == [0]
    assert (await world.integrate("B1")).outcome is Outcome.INTEGRATED
    assert world.trunk() == candidate


async def test_a_decision_about_one_story_leaves_the_work_of_another_alone(
    world: DecisionWorld,
) -> None:
    base = await world.awaiting_builder("B1")
    candidate = await world.reviewed("B1", on=base)

    await decided(world, "S2")

    assert [(each["subject"], each["story_id"]) for each in await world.owed()] == [("story", "S2")]
    assert (await world.integrate("B1")).outcome is Outcome.INTEGRATED
    assert world.trunk() == candidate


async def test_a_story_decision_leaves_the_roadmap_current_so_the_story_is_released_in_turn(
    world: DecisionWorld,
) -> None:
    await decided(world, "S2")
    await world.reconfirm_everything()
    gate, _ = await at_the_story_gate_of(world)

    await world.decide(gate)

    assert await world.story("S1") is StoryState.APPROVED
    assert await world.story("S2") is StoryState.RELEASED


async def at_the_story_gate_of(world: DecisionWorld) -> tuple[uuid.UUID, str]:
    """Story 1 delivered, sealed and assessed, in a world whose roadmap is approved."""
    candidate = await world.deliver("B1")
    await world.seal()
    await world.assess("B1 shows 120 for 100, as the story asks")
    return await world.gate(), candidate


async def test_an_assessment_written_before_an_answer_does_not_open_the_gate_after_it(
    world: DecisionWorld,
) -> None:
    await world.deliver("B1")
    await world.seal()
    revision, listed = await world.stories.revisions_of(PROJECT, "S1")
    read_by_the_analyst = await world.stories.seal_of(PROJECT, "S1")
    assert read_by_the_analyst is not None
    await decided(world)

    with pytest.raises(ActionRefused, match="not current"):
        await world.seal()
    await world.reconfirm_everything()
    with pytest.raises(ActionRefused, match="only a sealed list"):
        await world.assess("B1 shows it")
    await world.seal()

    with pytest.raises(ActionRefused, match="written on decision basis 0"):
        await world.stories.assess(
            ANALYST, PROJECT, "S1", "B1 shows it", story_revision=revision, list_revision=listed,
            basis=0, seal=read_by_the_analyst,
        )  # fmt: skip
    with pytest.raises(ActionRefused, match="no longer the story's"):
        await world.stories.assess(
            ANALYST, PROJECT, "S1", "B1 shows it", story_revision=revision, list_revision=listed,
            basis=1, seal=read_by_the_analyst,
        )  # fmt: skip
    assert await world.story("S1") is StoryState.RELEASED
    await world.assess("B1 shows it, zero-rated books aside")
    assert await world.story("S1") is StoryState.AWAITING_APPROVAL


async def test_an_answer_given_while_a_story_awaits_approval_puts_a_new_review_to_the_person(
    world: DecisionWorld,
) -> None:
    old_gate, _ = await at_the_story_gate_of(world)
    question = await world.ask("S1")
    with pytest.raises(ActionRefused, match="question"):
        await world.decide(old_gate)

    await world.answer(question)

    assert await world.story("S1") is StoryState.RELEASED
    assert await world.stories.inbox(PROJECT) == []
    assert (await world.decide(old_gate)).disposition == "stale"
    await world.reconfirm_everything()
    await world.seal()
    await world.assess("B1 still shows it, given the answer")
    new_gate = await world.gate()
    assert new_gate != old_gate
    [item] = await world.stories.inbox(PROJECT)
    assert [(each["source"], each["text"]) for each in item.manifest["decisions"]] == [
        ("product_owner", "zero-rated")
    ]
    assert (await world.decide(new_gate)).disposition == "effective"
    assert await world.story("S1") is StoryState.APPROVED


async def test_a_roadmap_waiting_for_approval_is_put_again_with_the_decision_shown(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    world = DecisionWorld(database, tmp_path, runner_image)
    await world.open()
    old_gate = await world.roadmap(STANDARD_RATE)
    await decided(world, story=None)
    assert (await world.decide(old_gate)).disposition == "stale"
    assert await world.stories.inbox(PROJECT) == []

    await world.reconfirm(await owed_for(world, "roadmap"))

    [item] = await world.stories.inbox(PROJECT)
    assert item.manifest["decisions"] == [
        {"number": 1, "source": "product_owner", "text": "zero-rated"}
    ]
    assert [story["story_id"] for story in item.manifest["stories"]] == ["S1"]
    await world.decide(item.gate_id)
    assert await world.owed() == []  # the story is created now, under the decision
    assert await world.story("S1") is StoryState.RELEASED


async def test_a_contract_is_not_reconfirmed_until_its_push_has_an_answer(
    world: DecisionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = await world.awaiting_builder("B1")
    candidate = await world.reviewed("B1", on=base)

    async def unreachable(*_: Any, **__: Any) -> None:
        raise RemoteUnavailable("no route to host")

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "_push", unreachable)
        assert (await world.integrate("B1")).outcome is Outcome.PENDING
        await decided(world)
        await world.reconfirm(await owed_for(world, "story"))
        owed = await owed_for(world, "behaviour")
        with pytest.raises(ActionRefused, match="has not settled yet"):
            await world.claims.claim(VERIFIER, owed["assignment_id"])
        async with world.database.connection() as connection:
            await connection.execute("UPDATE outbox SET published_at = now()")

    assert await world.integration.reconcile() == 1

    assert world.trunk() == candidate
    assert await world.stories.inbox(PROJECT) == []
    assert await world.column(
        "SELECT count(*) FROM outbox WHERE published_at IS NULL "
        "AND envelope->>'event_type' = 'reconfirmation.requested'"
    ) == [1]
    grant = await world.claims.claim(ANOTHER_VERIFIER, owed["assignment_id"])
    kept = await world.decisions.evidence_of(PROJECT, "B1")
    assert kept.integration is not None
    await world.decisions.reconfirm_contract(
        ANOTHER_VERIFIER, PROJECT, "B1", grant=grant, basis=1,
        contract_digest=CONTRACT.digest, evidence=kept,
    )  # fmt: skip
    await world.seal()
    await world.assess("B1 shows it")
    assert await world.story("S1") is StoryState.AWAITING_APPROVAL


async def test_a_decision_that_governs_nothing_any_more_is_not_replaced(
    world: DecisionWorld,
) -> None:
    await decided(world, "S1")
    [about_the_story] = (await world.decisions.context_for(PROJECT, "F1", "S1")).decisions
    await world.reconfirm_everything()
    gate, _ = await at_the_story_gate_of(world)
    await world.decide(gate)

    late = await world.replace(about_the_story.decision_id, "second thoughts")

    assert late.disposition == "stale"
    assert "correction" in late.outcome
    assert await world.owed() == []
    await decided(world, story=None)
    [feature_wide] = [
        each
        for each in (await world.decisions.context_for(PROJECT, "F1")).decisions
        if each.story_id is None
    ]
    await world.close("cancel", reason="dropped")
    assert (await world.replace(feature_wide.decision_id, "too late")).disposition == "stale"
    assert await world.owed() == []


async def test_the_decisions_and_the_basis_are_read_as_one(
    world: DecisionWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await decided(world)
    [decision] = (await world.decisions.context_for(PROJECT, "F1", "S1")).decisions
    recorded, go_on = asyncio.Event(), asyncio.Event()
    real = decisions_module._after_a_decision

    async def held_before_committing(*arguments: Any) -> None:
        await real(*arguments)
        recorded.set()
        await go_on.wait()

    monkeypatch.setattr(decisions_module, "_after_a_decision", held_before_committing)
    replacing = asyncio.create_task(world.replace(decision.decision_id, "standard rate"))
    await recorded.wait()
    reading = asyncio.create_task(world.decisions.context_for(PROJECT, "F1", "S1"))
    await world.until_someone_waits_for_a_lock()
    go_on.set()
    await replacing

    context = await reading
    assert (context.basis, [each.text for each in context.decisions]) == (2, ["standard rate"])
