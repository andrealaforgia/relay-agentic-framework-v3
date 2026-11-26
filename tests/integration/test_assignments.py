"""Assignments: each unit of work Relay wants done is a row of its own, announced by a
command and granted to one worker at a time. A grant is what a worker's first effect has
to name, so work that was replaced, withdrawn or paused cannot start late."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.assignments import Assignments
from relay.application.behaviours import Behaviours
from relay.application.reviews import Review, SubmissionRefused
from relay.application.stories import StoryDraft
from relay.domain.behaviour import BehaviourState
from relay.domain.evidence import GreenRejection, RedRejection
from relay.domain.story import ActionRefused, Principal, PrincipalKind
from relay.git.checkout import CheckoutFailed
from relay.git.mirror import Mirror
from relay.testing.oracle import ContainerIsolation, NativeIsolation, ProcessOracle
from tests.integration.fixtures import (
    ANALYST,
    BUILDER,
    CONTRACT,
    PRODUCT_OWNER,
    RELAY_ITSELF,
    VAT_APPLIES_STANDARD_RATE,
    VERIFIER,
    HeldAtABarrier,
    held_at_the_barrier,
    run_green,
    submit_review,
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
    return await world.column(
        "SELECT kind || ':' || state FROM assignments WHERE kind NOT IN ('plan', 'specify', 'red') ORDER BY created_at"
    )


async def test_a_verified_red_owes_one_build_and_the_command_says_which(
    world: StoryWorld,
) -> None:
    await world.awaiting_builder("B1")

    assert await assignments(world) == ["build:wanted"]
    assert [str(await the_build(world))] == await world.column(
        "SELECT assignment_id::text FROM assignments WHERE kind NOT IN ('plan', 'specify', 'red')"
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

    await world.behaviours.reopen_red(VERIFIER, PROJECT, "B1", "the runner was upgraded")

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

    assert await assignments(world) == [
        "build:done",
        "review:done",
        "review:done",
        "remediate:wanted",
    ]
    remediation = await the_build(world)
    assert remediation != build
    [spent] = await world.column("SELECT grant_id FROM assignments WHERE kind = 'build'")
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
    assert await assignments(world) == [
        "build:done",
        "review:done",
        "review:done",
        "remediate:wanted",
    ]


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
    await world.behaviours.propose(
        VERIFIER, PROJECT, "S1", "B1", "standard rate", ["AC1"], basis=0, story_revision=1
    )
    await world.behaviours.begin_test_design(VERIFIER, PROJECT, "B1")
    await world.behaviours.register_acceptance_test(VERIFIER, PROJECT, "B1", CONTRACT, basis=0)
    barrier, behaviours = held(world)
    running = asyncio.create_task(behaviours.run_red(RELAY_ITSELF, PROJECT, "B1"))
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
    await world.behaviours.propose(
        VERIFIER, PROJECT, "S1", "B1", "standard rate", ["AC1"], basis=0, story_revision=1
    )
    await world.behaviours.begin_test_design(VERIFIER, PROJECT, "B1")
    await world.behaviours.register_acceptance_test(VERIFIER, PROJECT, "B1", CONTRACT, basis=0)
    barrier, behaviours = held(world)
    running = asyncio.create_task(behaviours.run_red(RELAY_ITSELF, PROJECT, "B1"))
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


async def awaiting_review(world: StoryWorld) -> dict[str, tuple[str, uuid.UUID]]:
    """A candidate published for review. Returns each role's review and its assignment."""
    base = await world.awaiting_builder("B1")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    assert (await run_green(world.behaviours, PROJECT, "B1", 1)).is_valid
    assert await world.reviews.publish_candidates() == 1
    async with world.database.connection() as connection:
        tasks = await (
            await connection.execute("SELECT role, task_id, assignment_id FROM review_tasks")
        ).fetchall()
    return {task["role"]: (task["task_id"], task["assignment_id"]) for task in tasks}


def reviewer(role: str) -> Principal:
    return Principal(PrincipalKind.WORKER, f"worker-{role}", role=role)


async def test_a_published_candidate_owes_a_review_to_each_role_and_each_command_says_which(
    world: StoryWorld,
) -> None:
    reviews = await awaiting_review(world)

    assert await assignments(world) == ["build:done", "review:wanted", "review:wanted"]
    announced = await world.column(
        "SELECT envelope->'payload'->>'assignment_id' FROM outbox "
        "WHERE envelope->>'event_type' = 'review.requested'"
    )
    assert sorted(map(str, announced)) == sorted(str(owed) for _, owed in reviews.values())
    with pytest.raises(ActionRefused, match="for a qa"):
        await Assignments(world.database).claim(BUILDER, reviews["qa"][1])


async def test_a_review_checkout_has_to_name_the_grant_for_that_review(world: StoryWorld) -> None:
    reviews = await awaiting_review(world)
    claims = Assignments(world.database)
    task_id, owed = reviews["qa"]
    for_the_other_review = await claims.claim(reviewer("sentinel"), reviews["sentinel"][1])

    for not_current in (uuid.uuid7(), for_the_other_review):
        with pytest.raises(ActionRefused, match="not the current one"):
            await world.reviews.prepare_review(task_id, world.tmp_path / "tree", not_current)

    assert not (world.tmp_path / "tree").exists()
    assert await world.column("SELECT count(*) FROM review_checkouts") == [0]
    grant = await claims.claim(reviewer("qa"), owed)
    await world.reviews.prepare_review(task_id, world.tmp_path / "tree", grant)
    assert await world.column("SELECT count(*) FROM review_checkouts") == [1]


async def test_a_grant_taken_back_while_the_tree_was_being_fetched_starts_no_review(
    world: StoryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviews = await awaiting_review(world)
    task_id, owed = reviews["qa"]
    grant = await Assignments(world.database).claim(reviewer("qa"), owed)
    real = Mirror.export

    async def paused_and_resumed_during_the_export(self: Mirror, *arguments: Any) -> Path:
        tree = await real(self, *arguments)
        await world.close("pause", reason="one moment")
        await world.close("resume")
        return tree

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "export", paused_and_resumed_during_the_export)
        with pytest.raises(ActionRefused, match="not the current one"):
            await world.reviews.prepare_review(task_id, world.tmp_path / "tree", grant)

    assert await world.column("SELECT count(*) FROM review_checkouts") == [0]


async def test_a_review_granted_before_a_pause_is_granted_afresh_after_it_and_done_once(
    world: StoryWorld,
) -> None:
    reviews = await awaiting_review(world)
    claims = Assignments(world.database)
    task_id, owed = reviews["qa"]
    before = await claims.claim(reviewer("qa"), owed)
    await world.close("pause", reason="budget review")

    with pytest.raises(ActionRefused, match="paused"):
        await world.reviews.prepare_review(task_id, world.tmp_path / "tree", before)
    await world.close("resume")
    with pytest.raises(ActionRefused, match="not the current one"):
        await world.reviews.prepare_review(task_id, world.tmp_path / "tree", before)

    after = await claims.claim(reviewer("qa"), owed)
    await world.reviews.prepare_review(task_id, world.tmp_path / "tree", after)
    assert (await submit_review(world.reviews, task_id, Review())).disposition == "effective"
    assert await assignments(world) == ["build:done", "review:done", "review:wanted"]
    with pytest.raises(ActionRefused, match="no longer wanted"):
        await claims.claim(reviewer("qa"), owed)
    with pytest.raises(ActionRefused, match="not the current one"):
        await world.reviews.prepare_review(task_id, world.tmp_path / "once-more", after)
    assert not (world.tmp_path / "once-more").exists()


async def test_a_review_checked_out_before_a_pause_may_finish_during_it(world: StoryWorld) -> None:
    reviews = await awaiting_review(world)
    task_id, owed = reviews["qa"]
    grant = await Assignments(world.database).claim(reviewer("qa"), owed)
    await world.reviews.prepare_review(task_id, world.tmp_path / "tree", grant)

    await world.close("pause", reason="budget review")

    assert (await submit_review(world.reviews, task_id, Review())).disposition == "effective"
    assert await assignments(world) == ["build:done", "review:done", "review:wanted"]


async def test_a_review_of_a_cancelled_feature_is_kept_and_decides_nothing(
    world: StoryWorld,
) -> None:
    reviews = await awaiting_review(world)
    for role, (task_id, owed) in reviews.items():
        grant = await Assignments(world.database).claim(reviewer(role), owed)
        await world.reviews.prepare_review(task_id, world.tmp_path / f"tree-{role}", grant)
    await submit_review(world.reviews, reviews["qa"][0], Review())

    await world.close("cancel", reason="no longer wanted")
    late = await submit_review(world.reviews, reviews["sentinel"][0], Review())

    assert late.disposition == "stale"
    assert await world.state("B1") is BehaviourState.REVIEW
    assert await world.column("SELECT state FROM review_cycles") == ["open"]
    assert await assignments(world) == ["build:done", "review:done", "review:retired"]


async def test_a_cycle_that_can_no_longer_be_trusted_withdraws_its_reviews(
    world: StoryWorld,
) -> None:
    reviews = await awaiting_review(world)
    task_id, owed = reviews["qa"]
    grant = await Assignments(world.database).claim(reviewer("qa"), owed)
    [ref] = await world.column("SELECT candidate_ref FROM review_cycles")
    world.git("update-ref", "-d", str(ref), at=world.remote)

    with pytest.raises(SubmissionRefused, match="no longer names"):
        await world.reviews.prepare_review(task_id, world.tmp_path / "tree", grant)

    assert await assignments(world) == ["build:done", "review:retired", "review:retired"]


async def test_a_worker_whose_grant_was_taken_back_cannot_submit_on_its_successors_checkout(
    world: StoryWorld,
) -> None:
    reviews = await awaiting_review(world)
    claims = Assignments(world.database)
    task_id, owed = reviews["qa"]
    first = await claims.claim(reviewer("qa"), owed)
    await world.close("pause", reason="budget review")
    await world.close("resume")
    second_worker = Principal(PrincipalKind.WORKER, "worker-qa-2", role="qa")
    second = await claims.claim(second_worker, owed)
    handed_over = await world.reviews.prepare_review(task_id, world.tmp_path / "tree", second)

    for not_the_holder in (first, uuid.uuid7()):
        with pytest.raises(SubmissionRefused, match="no tree was prepared"):
            await world.reviews.submit_review(
                task_id, Review(), not_the_holder, handed_over.receipt
            )

    blocked = await world.reviews.submit_review(
        task_id, Review(findings=(INJECTION,)), second, handed_over.receipt
    )
    assert blocked.disposition == "effective"
    assert await world.column("SELECT count(*) FROM review_submissions") == [1]
    # An exact replay of what was accepted is answered, to the reviewer it was handed to
    # and to nobody else.
    replay = await world.reviews.submit_review(
        task_id, Review(findings=(INJECTION,)), second, handed_over.receipt
    )
    assert replay == blocked
    with pytest.raises(SubmissionRefused, match="no tree was prepared"):
        await world.reviews.submit_review(
            task_id, Review(findings=(INJECTION,)), first, uuid.uuid7()
        )


async def test_a_decided_review_answers_only_those_it_was_handed_to(world: StoryWorld) -> None:
    """3B-S2-3.1: once a review has an accepted submission, a caller that names it gets
    that submission's receipt for the same content, and a record of its disagreement for
    different content. Neither is for a caller the review's tree was never handed to
    under the grant it names: it would be writing a reviewer's evidence."""
    reviews = await awaiting_review(world)
    claims = Assignments(world.database)
    task_id, owed = reviews["qa"]
    grant = await claims.claim(reviewer("qa"), owed)
    handed = await world.reviews.prepare_review(task_id, world.tmp_path / "tree", grant)
    accepted = await world.reviews.submit_review(task_id, Review(), grant, handed.receipt)
    others_task, others_owed = reviews["sentinel"]
    others_grant = await claims.claim(reviewer("sentinel"), others_owed)
    theirs = await world.reviews.prepare_review(
        others_task, world.tmp_path / "theirs", others_grant
    )
    [recorded] = await world.column("SELECT count(*) FROM review_submissions")

    for presented in (
        (uuid.uuid7(), uuid.uuid7()),
        (others_grant, theirs.receipt),  # a real grant and receipt, of another review
        (grant, theirs.receipt),
        (others_grant, handed.receipt),
    ):
        for said in (Review(), Review(findings=(INJECTION,))):
            with pytest.raises(SubmissionRefused, match="no tree was prepared"):
                await world.reviews.submit_review(task_id, said, *presented)

    assert await world.column("SELECT count(*) FROM review_submissions") == [recorded]
    # The reviewer it was handed to is still answered, and its disagreement still kept.
    assert await world.reviews.submit_review(task_id, Review(), grant, handed.receipt) == accepted
    second_thoughts = await world.reviews.submit_review(
        task_id, Review(findings=(INJECTION,)), grant, handed.receipt
    )
    assert second_thoughts.disposition == "conflicting"
