"""What Relay does by itself after a pause, a resume or a restart: nothing is announced
for a closed feature, and whatever is still wanted is announced again, as the same
message, until someone has been granted it."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application import integration as integration_module
from relay.application import reviews as reviews_module
from relay.application.assignments import Assignments, announce_again
from relay.application.behaviours import Behaviours
from relay.application.features import Features
from relay.application.integration import (
    Integration,
    IntegrationRefused,
    Outcome,
    hand_on_unfinished_batches,
)
from relay.application.reviews import Review, Reviews, SubmissionRefused
from relay.application.stories import StoryDraft, close_feature
from relay.domain.behaviour import BehaviourState
from relay.domain.story import ActionRefused, HumanAction, Principal, PrincipalKind
from relay.git.checkout import RemoteUnavailable
from relay.git.mirror import Mirror
from relay.persistence.outbox import OutboxPublisher
from relay.testing.oracle import ContainerIsolation, NativeIsolation, ProcessOracle
from relay.transport.streams import Streams
from tests.integration.fixtures import (
    BUILDER,
    PRODUCT_OWNER,
    VAT_APPLIES_STANDARD_RATE,
    CountingLaunches,
    HeldAtABarrier,
    held_at_the_barrier,
    run_green,
    submit_review,
)
from tests.integration.story_world import GREETS, STANDARD_RATE, StoryWorld
from tests.integration.trunk_world import PROJECT, ZERO_STAYS_ZERO


class Delivered(Streams):
    """Stands where Redis would, and remembers what reached it."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, uuid.UUID]] = []
        self.accepted = asyncio.Event()
        self.go_on = asyncio.Event()
        self.go_on.set()

    async def append(self, stream: str, message_id: uuid.UUID, envelope: dict[str, Any]) -> None:
        self.messages.append((envelope["event_type"], message_id))
        self.accepted.set()
        await self.go_on.wait()

    def of(self, event_type: str) -> list[uuid.UUID]:
        return [message for kind, message in self.messages if kind == event_type]


@pytest.fixture
async def world(database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> StoryWorld:
    world = StoryWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await world.decide(await world.roadmap(STANDARD_RATE, GREETS))
    return world


async def again(world: StoryWorld) -> int:
    async with world.database.connection() as connection, connection.transaction():
        return await announce_again(connection, PROJECT)


@pytest.mark.parametrize("closure", ["pause", "cancel"])
async def test_the_commands_of_a_closed_feature_stay_in_the_outbox(
    world: StoryWorld, closure: str
) -> None:
    await world.awaiting_builder("B1")
    await world.close(closure, reason="stop")
    delivered = Delivered()
    publisher = OutboxPublisher(world.database, delivered)

    await publisher.publish_pending()

    assert delivered.of("build.requested") == []
    if closure == "pause":
        await world.close("resume")
        await publisher.publish_pending()
        assert len(delivered.of("build.requested")) == 1


async def test_work_still_wanted_is_announced_again_as_the_same_message(world: StoryWorld) -> None:
    await world.awaiting_builder("B1")
    delivered = Delivered()
    publisher = OutboxPublisher(world.database, delivered)
    await publisher.publish_pending()

    assert await again(world) == 1
    await publisher.publish_pending()

    [first, second] = delivered.of("build.requested")
    assert first == second
    [build] = await world.column("SELECT assignment_id FROM assignments")
    assert isinstance(build, uuid.UUID)
    await Assignments(world.database).claim(BUILDER, build)
    assert await again(world) == 0
    await world.close("pause", reason="stop")  # takes the grant back: wanted, but closed
    assert await again(world) == 0
    await world.close("cancel", reason="dropped")
    assert await again(world) == 0
    await publisher.publish_pending()
    assert len(delivered.of("build.requested")) == 2


async def test_announcing_again_while_the_first_announcement_is_being_sent_is_not_lost(
    world: StoryWorld,
) -> None:
    """The publisher has handed the command to the broker and not yet marked it sent. If
    the broker then loses it, the request to announce again must still stand."""
    await world.awaiting_builder("B1")
    delivered = Delivered()
    publisher = OutboxPublisher(world.database, delivered)
    await publisher.publish_pending()  # everything else is out of the way
    async with world.database.connection() as connection:
        await connection.execute(
            "UPDATE outbox SET published_at = NULL WHERE envelope->>'event_type' = 'build.requested'"
        )
    delivered.accepted.clear()
    delivered.go_on.clear()
    publishing = asyncio.create_task(publisher.publish_pending())
    await delivered.accepted.wait()

    asking = asyncio.create_task(again(world))
    await world.until_someone_waits_for_a_lock()
    delivered.go_on.set()
    await publishing
    assert await asking == 1

    await publisher.publish_pending()
    assert len(set(delivered.of("build.requested"))) == 1
    assert len(delivered.of("build.requested")) == 3


async def checks_in_flight(
    world: StoryWorld, request: str = "once"
) -> tuple[str, HeldAtABarrier, asyncio.Task[Any]]:
    """A reviewed candidate whose regression check is running. Returns the candidate."""
    await world.integration.require_check(PROJECT, ZERO_STAYS_ZERO)
    base = await world.awaiting_builder("B1")
    candidate = await world.reviewed("B1", on=base)
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    running = asyncio.create_task(world.integration_with(barrier).integrate(PROJECT, "B1", request))
    await held_at_the_barrier(running, barrier)
    return candidate, barrier, running


async def batches(world: StoryWorld) -> list[object]:
    return await world.column(
        "SELECT request_id || ':' || generation || ':' || state FROM check_batches "
        "ORDER BY registered"
    )


async def killed(task: asyncio.Task[Any]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def restarted(world: StoryWorld) -> Features:
    """Another control-plane process over the same database, mirrors and repository."""
    return Features(
        world.database, Reviews(world.database, world.mirrors), world.integration_with()
    )


@pytest.fixture
def dies_without_tidying(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process that is killed abandons nothing: its batch is simply left running."""

    async def nothing(self: Integration, batch_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(Integration, "_abandon", nothing)


async def test_checks_held_by_a_pause_run_again_after_the_resume_and_integrate_once(
    world: StoryWorld,
) -> None:
    candidate, barrier, running = await checks_in_flight(world)
    await world.close("pause", reason="budget review")
    barrier.release.set()
    assert (await running).outcome is Outcome.HELD

    await world.close("resume")

    assert world.trunk() == candidate
    assert await batches(world) == ["once:1:interrupted", "once:2:authorised"]
    assert await world.column("SELECT state FROM integration_intents") == ["integrated"]
    assert await world.column("SELECT state FROM assignments WHERE kind = 'integrate'") == ["done"]
    assert (await world.integration.integrate(PROJECT, "B1", "once")).outcome is Outcome.INTEGRATED
    await world.features.reconcile(PROJECT)
    await world.features.start()
    assert await batches(world) == ["once:1:interrupted", "once:2:authorised"]
    assert await world.column("SELECT count(*) FROM executions WHERE purpose = 'check'") == [2]


async def held_then_resumed_without_the_pass(world: StoryWorld) -> tuple[str, uuid.UUID]:
    """Checks held by a pause, the feature resumed, and the integration that is then
    owed not yet taken up by anyone. Returns the candidate and that assignment."""
    candidate, barrier, running = await checks_in_flight(world, "first")
    await world.close("pause", reason="budget review")
    barrier.release.set()
    await running
    resume = HumanAction(PRODUCT_OWNER, "resume-1", "F1", "resume", {})
    await close_feature(world.database, PROJECT, resume)
    async with world.database.connection() as connection, connection.transaction():
        await hand_on_unfinished_batches(connection, PROJECT, starting=False)
    [owed] = await world.column("SELECT assignment_id FROM assignments WHERE kind = 'integrate'")
    assert isinstance(owed, uuid.UUID)
    return candidate, owed


async def test_an_integration_owed_for_checks_that_were_since_replaced_starts_nothing(
    world: StoryWorld,
) -> None:
    candidate, owed = await held_then_resumed_without_the_pass(world)
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    newer = asyncio.create_task(world.integration_with(barrier).integrate(PROJECT, "B1", "second"))
    await held_at_the_barrier(newer, barrier)

    late = await world.integration.continue_integration(owed)

    assert late.outcome is Outcome.SUPERSEDED
    assert await batches(world) == ["first:1:interrupted", "second:1:running"]
    assert await world.column("SELECT state FROM assignments WHERE kind = 'integrate'") == [
        "retired"
    ]
    assert (await world.events())[-1] == "integration.successor_stale"
    barrier.release.set()
    assert (await newer).outcome is Outcome.INTEGRATED
    assert world.trunk() == candidate
    assert (await world.integration.continue_integration(owed)).outcome is Outcome.HELD


async def test_an_integration_is_not_taken_up_while_its_feature_is_paused(
    world: StoryWorld,
) -> None:
    candidate, owed = await held_then_resumed_without_the_pass(world)
    await world.close("pause", reason="again")

    with pytest.raises(ActionRefused, match="paused"):
        await world.integration.continue_integration(owed)

    assert await batches(world) == ["first:1:interrupted"]
    await world.close("resume")
    assert world.trunk() == candidate


async def test_the_pass_after_a_resume_leaves_checks_that_are_running_alone(
    world: StoryWorld,
) -> None:
    candidate, barrier, running = await checks_in_flight(world)

    await world.features.reconcile(PROJECT)

    assert await batches(world) == ["once:1:running"]
    barrier.release.set()
    assert (await running).outcome is Outcome.INTEGRATED
    assert world.trunk() == candidate


async def test_work_announced_before_a_restart_is_announced_again_after_it(
    world: StoryWorld,
) -> None:
    await world.awaiting_builder("B1")
    delivered = Delivered()
    publisher = OutboxPublisher(world.database, delivered)
    await publisher.publish_pending()

    await restarted(world).start()  # the broker may have lost everything
    await publisher.publish_pending()

    [first, second] = delivered.of("build.requested")
    assert first == second


@pytest.mark.parametrize("closure", ["pause", "cancel"])
async def test_a_push_authorised_before_a_closure_is_settled_by_the_next_start(
    world: StoryWorld, closure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = await world.awaiting_builder("B1")
    candidate = await world.reviewed("B1", on=base)

    async def unreachable(*_: Any, **__: Any) -> None:
        raise RemoteUnavailable("no route to host")

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "_push", unreachable)
        assert (await world.integrate("B1")).outcome is Outcome.PENDING
    await world.close(closure, reason="stop")

    await restarted(world).start()

    assert world.trunk() == candidate
    assert await world.column("SELECT state FROM integration_intents") == ["integrated"]
    assert await world.state("B1") is BehaviourState.INTEGRATED
    assert (await world.events()).count("behaviour.integrated") == 1


@pytest.mark.parametrize(
    ("closure", "found", "after"),
    [
        (None, "once:1:interrupted", Outcome.INTEGRATED),
        ("pause", "once:1:held", Outcome.INTEGRATED),
        ("cancel", "once:1:abandoned", Outcome.SUPERSEDED),
    ],
)
@pytest.mark.usefixtures("dies_without_tidying")
async def test_checks_left_running_by_a_process_that_died_are_closed_when_the_next_one_starts(
    world: StoryWorld, closure: str | None, found: str, after: Outcome
) -> None:
    candidate, _, running = await checks_in_flight(world)
    if closure:
        await world.close(closure, reason="stop")
    await killed(running)
    assert await batches(world) == ["once:1:running"]

    await restarted(world).start()

    assert (await batches(world))[0] == found
    if closure == "pause":
        assert world.trunk() != candidate
        await world.close("resume")
    assert (await world.integration.integrate(PROJECT, "B1", "once")).outcome is after
    assert (world.trunk() == candidate) is (closure != "cancel")
    assert await world.column("SELECT count(*) FROM integration_intents") == [
        0 if closure == "cancel" else 1
    ]
    assert len(await batches(world)) == (1 if closure == "cancel" else 2)


async def test_a_review_checked_out_by_a_process_that_died_is_granted_afresh_and_done_once(
    world: StoryWorld,
) -> None:
    base = await world.awaiting_builder("B1")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    assert (await run_green(world.behaviours, PROJECT, "B1", 1)).is_valid
    assert await world.reviews.publish_candidates() == 1
    async with world.database.connection() as connection:
        task = await (
            await connection.execute(
                "SELECT task_id, assignment_id FROM review_tasks WHERE role = 'qa'"
            )
        ).fetchone()
    assert task is not None
    qa = Principal(PrincipalKind.WORKER, "worker-qa", role="qa")
    claims = Assignments(world.database)
    before = await claims.claim(qa, task["assignment_id"])
    handed_over = await world.reviews.prepare_review(
        task["task_id"], world.tmp_path / "tree", before
    )

    await restarted(world).start()

    with pytest.raises(SubmissionRefused, match="no tree was prepared"):
        # The tree was handed over under a grant that holds nothing now.
        await world.reviews.submit_review(task["task_id"], Review(), before, handed_over.receipt)
    with pytest.raises(ActionRefused, match="not the current one"):
        await world.reviews.prepare_review(task["task_id"], world.tmp_path / "late", before)
    after = await claims.claim(
        Principal(PrincipalKind.WORKER, "worker-qa-2", role="qa"), task["assignment_id"]
    )
    await world.reviews.prepare_review(task["task_id"], world.tmp_path / "again", after)
    assert (await submit_review(world.reviews, task["task_id"], Review())).disposition == (
        "effective"
    )
    assert await world.column("SELECT count(*) FROM review_checkouts") == [2]
    assert await world.column("SELECT count(*) FROM review_submissions") == [1]


async def test_a_green_run_cut_short_by_a_process_that_died_leaves_the_build_to_be_claimed_again(
    world: StoryWorld,
) -> None:
    base = await world.awaiting_builder("B1")
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    [build] = await world.column("SELECT assignment_id FROM assignments")
    assert isinstance(build, uuid.UUID)
    claims = Assignments(world.database)
    before = await claims.claim(BUILDER, build)
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    dying = Behaviours(world.database, world.mirrors, ProcessOracle(barrier))
    running = asyncio.create_task(dying.run_green(PROJECT, "B1", 1, before))
    await held_at_the_barrier(running, barrier)
    await killed(running)

    await restarted(world).start()

    assert await world.column("SELECT state FROM green_runs") == ["interrupted"]
    assert await world.column("SELECT state FROM assignments") == ["wanted"]
    with pytest.raises(ActionRefused, match="not the current one"):
        await world.behaviours.run_green(PROJECT, "B1", 1, before)
    fresh = await claims.claim(BUILDER, build)
    await world.close("pause", reason="one moment")  # the fresh grant has done nothing yet
    assert await world.column("SELECT state FROM assignments") == ["wanted"]
    await world.close("resume")
    after = await claims.claim(BUILDER, build)
    assert after != fresh
    assert (await world.behaviours.run_green(PROJECT, "B1", 1, after)).is_valid
    assert await world.state("B1") is BehaviourState.REVIEW


async def test_checks_held_by_a_pause_stay_held_however_often_relay_starts(
    world: StoryWorld,
) -> None:
    _, barrier, running = await checks_in_flight(world)
    await world.close("pause", reason="budget review")
    barrier.release.set()
    await running
    before = await world.events()

    await restarted(world).start()
    await restarted(world).start()

    assert await batches(world) == ["once:1:held"]
    assert await world.events() == before


async def test_a_command_already_on_its_way_when_a_pause_lands_cannot_be_claimed_until_resume(
    world: StoryWorld,
) -> None:
    await world.awaiting_builder("B1")
    delivered = Delivered()
    delivered.go_on.clear()
    publishing = asyncio.create_task(OutboxPublisher(world.database, delivered).publish_pending())
    await delivered.accepted.wait()
    while not delivered.of("build.requested"):
        delivered.go_on.set()
        await asyncio.sleep(0)
        delivered.go_on.clear()
    await world.close("pause", reason="budget review")
    delivered.go_on.set()
    await publishing
    [build] = await world.column("SELECT assignment_id FROM assignments")
    assert isinstance(build, uuid.UUID)

    with pytest.raises(ActionRefused, match="paused"):
        await Assignments(world.database).claim(BUILDER, build)

    await world.close("resume")
    await Assignments(world.database).claim(BUILDER, build)


async def integrations_owed(world: StoryWorld) -> list[object]:
    return await world.column(
        "SELECT state FROM assignments WHERE kind = 'integrate' AND project_id = "
        f"'{world.project}' ORDER BY created_at"
    )


async def test_an_integration_is_not_taken_up_where_its_checks_could_open_no_gate(
    world: StoryWorld,
) -> None:
    candidate, owed = await held_then_resumed_without_the_pass(world)
    native = CountingLaunches(NativeIsolation())

    with pytest.raises(IntegrationRefused, match="container"):
        await world.integration_with(native).continue_integration(owed)

    await Features(world.database, world.reviews, world.integration_with(native)).reconcile(
        PROJECT
    )  # a pass in such a process leaves it owed, and is not stopped by it
    assert native.launches == 0
    assert await integrations_owed(world) == ["wanted"]
    assert await batches(world) == ["first:1:interrupted"]
    assert (await world.integration.continue_integration(owed)).outcome is Outcome.INTEGRATED
    assert world.trunk() == candidate


async def test_no_worker_can_claim_an_integration_the_control_plane_takes_up_itself(
    world: StoryWorld,
) -> None:
    candidate, owed = await held_then_resumed_without_the_pass(world)
    integrator = Principal(PrincipalKind.WORKER, "worker-integrator", role="integrator")

    with pytest.raises(ActionRefused, match="control plane"):
        await Assignments(world.database).claim(integrator, owed)

    await world.features.reconcile(PROJECT)
    assert await batches(world) == ["first:1:interrupted", "first:2:authorised"]
    assert world.trunk() == candidate


async def test_starting_leaves_a_paused_features_integration_waiting_and_sees_to_the_rest(
    world: StoryWorld, database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    (tmp_path / "later").mkdir()
    later = StoryWorld(database, tmp_path / "later", runner_image, project="zeta")
    await later.open()
    await later.decide(await later.roadmap(STANDARD_RATE))
    await later.awaiting_builder("B1")
    [build] = await world.column("SELECT assignment_id FROM assignments WHERE project_id = 'zeta'")
    assert isinstance(build, uuid.UUID)
    await Assignments(database).claim(BUILDER, build)
    candidate, _ = await held_then_resumed_without_the_pass(world)
    await world.close("pause", reason="again")

    await restarted(world).start()

    assert await integrations_owed(world) == ["wanted"]
    assert await world.column("SELECT state FROM assignments WHERE project_id = 'zeta'") == [
        "wanted"
    ]
    await world.close("resume")
    assert world.trunk() == candidate


async def test_a_pause_that_lands_as_the_pass_takes_up_an_integration_does_not_stop_the_pass(
    world: StoryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, _ = await held_then_resumed_without_the_pass(world)
    real = Integration.continue_integration

    async def paused_first(self: Integration, assignment_id: uuid.UUID) -> Any:
        await world.close("pause", reason="just now")
        return await real(self, assignment_id)

    with monkeypatch.context() as patched:
        patched.setattr(Integration, "continue_integration", paused_first)
        await world.features.reconcile(PROJECT)

    assert await integrations_owed(world) == ["wanted"]
    await world.close("resume")
    assert world.trunk() == candidate


async def test_an_integration_cut_short_while_it_was_being_taken_up_is_still_owed(
    world: StoryWorld,
) -> None:
    candidate, _ = await held_then_resumed_without_the_pass(world)
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    features = Features(world.database, world.reviews, world.integration_with(barrier))
    passing = asyncio.create_task(features.reconcile(PROJECT))
    await held_at_the_barrier(passing, barrier)

    await killed(passing)  # an ordinary cancellation: the tidying up runs

    assert await batches(world) == ["first:1:interrupted", "first:2:interrupted"]
    assert await integrations_owed(world) == ["done", "wanted"]
    await world.close("resume")  # changes nothing, and runs the pass
    assert await batches(world) == [
        "first:1:interrupted",
        "first:2:interrupted",
        "first:3:authorised",
    ]
    assert world.trunk() == candidate
    assert await world.column("SELECT count(*) FROM integration_intents") == [1]


async def test_the_latest_checks_are_the_ones_registered_last_not_the_ones_begun_last(
    world: StoryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One request opens its transaction first and reaches the behaviour second. It is
    the later registration, it replaces the other's checks, and it is what a pause holds:
    its successor must run, though its transaction is the older of the two."""
    await world.integration.require_check(PROJECT, ZERO_STAYS_ZERO)
    base = await world.awaiting_builder("B1")
    candidate = await world.reviewed("B1", on=base)
    opened, go_on = asyncio.Event(), asyncio.Event()
    real = integration_module.lock_gate

    async def first_caller_waits(connection: Any, project_id: str, **options: Any) -> None:
        await real(connection, project_id, **options)
        if not opened.is_set():
            opened.set()
            await go_on.wait()

    monkeypatch.setattr(integration_module, "lock_gate", first_caller_waits)
    slow_barrier = HeldAtABarrier(ContainerIsolation(world.image))
    slow = asyncio.create_task(
        world.integration_with(slow_barrier).integrate(PROJECT, "B1", "slow")
    )
    await opened.wait()
    quick_barrier = HeldAtABarrier(ContainerIsolation(world.image))
    quick = asyncio.create_task(
        world.integration_with(quick_barrier).integrate(PROJECT, "B1", "quick")
    )
    await held_at_the_barrier(quick, quick_barrier)
    go_on.set()
    await held_at_the_barrier(slow, slow_barrier)
    quick_barrier.release.set()
    assert (await quick).outcome is Outcome.SUPERSEDED
    await world.close("pause", reason="budget review")
    slow_barrier.release.set()
    assert (await slow).outcome is Outcome.HELD

    await world.close("resume")

    assert await batches(world) == ["quick:1:superseded", "slow:1:interrupted", "slow:2:authorised"]
    assert world.trunk() == candidate


async def test_checks_held_by_a_pause_are_abandoned_when_the_feature_is_then_cancelled(
    world: StoryWorld,
) -> None:
    _, barrier, running = await checks_in_flight(world)
    await world.close("pause", reason="budget review")
    barrier.release.set()
    await running

    await world.close("cancel", reason="dropped after all")

    assert await batches(world) == ["once:1:abandoned"]
    again = await world.integration.integrate(PROJECT, "B1", "once")
    assert again.outcome is Outcome.SUPERSEDED
    assert "cancelled" in again.reason


async def test_cancelling_one_feature_leaves_the_held_checks_of_another_alone(
    world: StoryWorld,
) -> None:
    candidate, barrier, running = await checks_in_flight(world)
    await world.close("pause", reason="budget review")
    barrier.release.set()
    await running
    another = StoryDraft("T1", "Another feature", "something else", {"AC1": "it works"})
    await world.roadmap(another, feature="F2")

    await world.close("cancel", feature="F2", reason="dropped")

    assert await batches(world) == ["once:1:held"]
    await world.close("resume")
    assert world.trunk() == candidate


async def test_an_integration_cut_short_while_its_checks_were_being_settled_is_still_owed(
    world: StoryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, _ = await held_then_resumed_without_the_pass(world)
    settling = asyncio.Event()

    async def never_returns(self: Integration, *arguments: Any) -> Any:
        settling.set()
        await asyncio.Event().wait()

    with monkeypatch.context() as patched:
        patched.setattr(Integration, "_settle_batch", never_returns)
        passing = asyncio.create_task(world.features.reconcile(PROJECT))
        await settling.wait()
        await killed(passing)

    assert await batches(world) == ["first:1:interrupted", "first:2:interrupted"]
    assert await integrations_owed(world) == ["done", "wanted"]
    await world.close("resume")
    assert (await batches(world))[-1] == "first:3:authorised"
    assert world.trunk() == candidate
    assert await world.column("SELECT count(*) FROM integration_intents") == [1]


async def test_checks_taken_up_by_relay_and_overtaken_by_a_new_required_check_are_run_again(
    world: StoryWorld,
) -> None:
    candidate, _ = await held_then_resumed_without_the_pass(world)
    barrier = HeldAtABarrier(ContainerIsolation(world.image))
    features = Features(world.database, world.reviews, world.integration_with(barrier))
    passing = asyncio.create_task(features.reconcile(PROJECT))
    await held_at_the_barrier(passing, barrier)

    await world.integration.require_check(
        PROJECT, replace(ZERO_STAYS_ZERO, contract_id="zero-still-stays-zero")
    )
    barrier.release.set()
    await passing  # the one pass sees the new check set through, with nothing else called

    assert await batches(world) == [
        "first:1:interrupted",
        "first:2:interrupted",
        "first:3:authorised",
    ]
    [why] = await world.column("SELECT reason FROM check_batches WHERE generation = 2")
    assert "checks required" in str(why)
    assert world.trunk() == candidate
    assert await world.column("SELECT count(*) FROM integration_intents") == [1]


async def test_a_paused_features_integration_costs_the_pass_no_call_to_the_repository(
    world: StoryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, owed = await held_then_resumed_without_the_pass(world)
    await world.close("pause", reason="again")
    asked: list[float | None] = []

    async def stalls(repository: str, trunk: str, *, timeout: float | None = None) -> str:
        asked.append(timeout)
        raise RemoteUnavailable("no answer in time")

    monkeypatch.setattr(integration_module, "trunk_head", stalls)
    await restarted(world).start()
    with pytest.raises(ActionRefused, match="paused"):
        await world.integration.continue_integration(owed)
    assert asked == []

    await world.close("resume")  # open now: the read is made, bounded, and may fail

    assert asked == [120.0]
    assert await integrations_owed(world) == ["wanted"]
    monkeypatch.undo()
    await world.features.reconcile(PROJECT)
    assert world.trunk() == candidate


async def test_cancelling_a_feature_ends_the_request_whose_checks_were_waiting_to_run_again(
    world: StoryWorld,
) -> None:
    await held_then_resumed_without_the_pass(world)

    await world.close("cancel", reason="dropped")

    assert await batches(world) == ["first:1:abandoned"]
    assert await integrations_owed(world) == ["retired"]
    again = await world.integration.integrate(PROJECT, "B1", "first")
    assert again.outcome is Outcome.SUPERSEDED


class ChangesTheChecksEveryRun:
    """Wraps an isolation so that each run is overtaken by a newly required check."""

    def __init__(self, world: StoryWorld) -> None:
        self._isolation = ContainerIsolation(world.image)
        self.name = self._isolation.name
        self._world = world
        self.runs = 0

    async def run(self, workdir: Path, launch: Any, capture_limit: int) -> Any:
        run = await self._isolation.run(workdir, launch, capture_limit)
        self.runs += 1
        await self._world.integration.require_check(
            PROJECT, replace(ZERO_STAYS_ZERO, contract_id=f"zero-stays-zero-{self.runs}")
        )
        return run


async def test_a_pass_does_not_chase_checks_that_keep_being_overtaken_for_ever(
    world: StoryWorld,
) -> None:
    candidate, _ = await held_then_resumed_without_the_pass(world)
    restless = world.integration_with(ChangesTheChecksEveryRun(world))

    await Features(world.database, world.reviews, restless).reconcile(PROJECT)

    assert world.trunk() != candidate
    assert len(await batches(world)) == 4, "the held batch, then three attempts in one pass"
    assert (await integrations_owed(world))[-1] == "wanted"


async def test_one_projects_silent_repository_does_not_keep_the_pass_from_another_project(
    world: StoryWorld,
    database: AsyncConnectionPool,
    tmp_path: Path,
    runner_image: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, _ = await held_then_resumed_without_the_pass(world)
    (tmp_path / "silent").mkdir()
    silent = StoryWorld(
        database, tmp_path / "silent", runner_image, project="alpha", mirrors=world.mirrors
    )
    await silent.open()
    await silent.decide(await silent.roadmap(STANDARD_RATE))
    base = await silent.awaiting_builder("B1")
    silent.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    assert (await run_green(silent.behaviours, "alpha", "B1", 1)).is_valid
    real = reviews_module.resolve_ref

    async def alpha_never_answers(
        repository: str, wanted: str, *, timeout: float | None = None
    ) -> str | None:
        if "silent" not in repository:
            return await real(repository, wanted, timeout=timeout)
        await asyncio.sleep(3600 if timeout is None else timeout)
        raise RemoteUnavailable("no answer in time")

    monkeypatch.setattr(reviews_module, "resolve_ref", alpha_never_answers)
    impatient = Reviews(world.database, world.mirrors, remote_timeout=0.2)

    async with asyncio.timeout(90):
        await Features(world.database, impatient, world.integration).reconcile(PROJECT)

    assert world.trunk() == candidate
    assert await world.column(
        "SELECT publication FROM review_cycles WHERE project_id = 'alpha'"
    ) == ["pending"]
    assert "review.publication_deferred" in [
        str(kind)
        for kind in await world.column("SELECT event_type FROM events WHERE project_id = 'alpha'")
    ]
