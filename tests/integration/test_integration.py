"""Scenario 7: a reviewed candidate reaches trunk only after every required check has been
observed on that exact commit, and trunk moves once, to that commit, or not at all."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application import integration as integration_module
from relay.application.integration import IntegrationRefused, Outcome
from relay.domain.behaviour import BehaviourState
from relay.domain.evidence import Launch, RedRejection
from relay.git import checkout as checkout_module
from relay.git import mirror as mirror_module
from relay.git.checkout import CheckoutFailed, RemoteUnavailable
from relay.git.mirror import Mirror, NotFastForward
from relay.persistence.outbox import pending_messages
from relay.testing.oracle import ContainerIsolation, Isolation, NativeIsolation, Run
from tests.integration.fixtures import (
    CONTRACT,
    VAT_APPLIES_STANDARD_RATE,
    CountingLaunches,
    HeldAtABarrier,
    held_at_the_barrier,
)
from tests.integration.trunk_world import (
    GREETING,
    PROJECT,
    VAT_ALWAYS_PRINTS_120,
    ZERO_STAYS_ZERO,
    TrunkWorld,
)


@pytest.fixture
async def world(database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> TrunkWorld:
    world = TrunkWorld(database, tmp_path, runner_image)
    await world.open()
    return world


async def test_a_reviewed_candidate_that_passes_every_check_becomes_trunk(
    world: TrunkWorld,
) -> None:
    await world.integration.require_check(PROJECT, ZERO_STAYS_ZERO)
    base = await world.awaiting_builder()
    candidate = await world.reviewed(on=base)

    receipt = await world.integrate()

    assert receipt.outcome is Outcome.INTEGRATED
    assert world.trunk() == candidate
    assert await world.state() is BehaviourState.INTEGRATED
    assert (await world.events())[-3:] == [
        "integration.checks_started",
        "integration.authorised",
        "behaviour.integrated",
    ]
    assert await world.column("SELECT state FROM integration_intents") == ["integrated"]
    assert await world.column("SELECT state FROM review_cycles") == ["integrated"]


async def test_a_failing_required_check_keeps_the_candidate_off_trunk(world: TrunkWorld) -> None:
    await world.integration.require_check(PROJECT, ZERO_STAYS_ZERO)
    base = await world.awaiting_builder()
    await world.reviewed(files={"vat.py": VAT_ALWAYS_PRINTS_120}, on=base)

    receipt = await world.integrate()

    assert receipt.outcome is Outcome.CHECKS_FAILED
    assert world.trunk() == base
    assert await world.state() is BehaviourState.IMPLEMENTING
    assert await world.column("SELECT state FROM review_cycles") == ["changes_requested"]
    assert await world.column("SELECT count(*) FROM integration_intents") == [0]
    [failed] = await world.column("SELECT failed_checks FROM remediation_requests")
    assert isinstance(failed, list)
    assert [check["contract_id"] for check in failed] == ["zero-stays-zero"]
    [execution] = await world.column(
        "SELECT execution_id::text FROM executions WHERE purpose = 'check'"
    )
    assert failed[0]["execution_id"] == execution
    remediations = [
        message.envelope["payload"]["failed_checks"]
        for message in await pending_messages(world.database)
        if "failed_checks" in message.envelope["payload"]
    ]
    assert remediations == [["zero-stays-zero"]]


async def test_a_candidate_whose_reviews_are_not_all_in_is_refused_before_anything_runs(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    counting = CountingLaunches(ContainerIsolation(runner_image))
    world = TrunkWorld(database, tmp_path, runner_image, counting)
    await world.open()
    await world.integration.require_check(PROJECT, ZERO_STAYS_ZERO)
    base = await world.awaiting_builder()
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    assert (await world.behaviours.run_green(PROJECT, "B1", 1)).is_valid
    launched = counting.launches

    with pytest.raises(IntegrationRefused, match="review"):
        await world.integrate()

    assert counting.launches == launched
    assert world.trunk() == base
    assert await world.column("SELECT count(*) FROM check_batches") == [0]


async def test_checks_observed_outside_a_container_are_refused_before_anything_runs(
    world: TrunkWorld,
) -> None:
    base = await world.awaiting_builder()
    await world.reviewed(on=base)
    counting = CountingLaunches(NativeIsolation())
    native = TrunkWorld(world.database, world.tmp_path, world.image, counting)

    with pytest.raises(IntegrationRefused, match="container"):
        await native.integrate()

    assert counting.launches == 0
    assert world.trunk() == base


async def test_what_an_integrated_behaviour_promised_must_hold_for_every_later_candidate(
    world: TrunkWorld,
) -> None:
    base = await world.awaiting_builder()
    await world.reviewed(on=base)
    assert (await world.integrate()).outcome is Outcome.INTEGRATED
    trunk = await world.awaiting_builder("B2", GREETING)
    await world.reviewed(
        "B2", {"greeting.txt": "hello\n", "vat.py": "print('rewritten')\n"}, on=trunk
    )

    receipt = await world.integrate("B2")

    assert receipt.outcome is Outcome.CHECKS_FAILED
    assert world.trunk() == trunk
    [failed] = await world.column("SELECT failed_checks FROM remediation_requests")
    assert isinstance(failed, list)
    assert [check["contract_id"] for check in failed] == ["B1-standard-rate"]


async def test_a_second_behaviour_integrates_on_top_of_the_first(world: TrunkWorld) -> None:
    base = await world.awaiting_builder()
    await world.reviewed(on=base)
    await world.integrate()
    trunk = await world.awaiting_builder("B2", GREETING)
    candidate = await world.reviewed("B2", {"greeting.txt": "hello\n"}, on=trunk)

    receipt = await world.integrate("B2")

    assert receipt.outcome is Outcome.INTEGRATED
    assert world.trunk() == candidate
    assert await world.column("SELECT count(*) FROM executions WHERE purpose = 'check'") == [1]


async def test_a_check_cannot_be_registered_twice_under_one_name(world: TrunkWorld) -> None:
    await world.integration.require_check(PROJECT, ZERO_STAYS_ZERO)

    with pytest.raises(ValueError, match="already"):
        await world.integration.require_check(PROJECT, ZERO_STAYS_ZERO)

    assert await world.column("SELECT check_policy_version FROM projects") == [1]


class Falsified:
    """Reports every run as a failure, whatever the subject did."""

    def __init__(self, isolation: Isolation) -> None:
        self._isolation = isolation
        self.name = isolation.name

    async def run(self, workdir: Path, launch: Launch, capture_limit: int) -> Run:
        run = await self._isolation.run(workdir, launch, capture_limit)
        return replace(run, observation=replace(run.observation, exit_code=1))


class AnotherRuntime:
    """The same runs, observed in what claims to be a different runtime."""

    def __init__(self, isolation: Isolation) -> None:
        self._isolation = isolation
        self.name = isolation.name

    async def run(self, workdir: Path, launch: Launch, capture_limit: int) -> Run:
        run = await self._isolation.run(workdir, launch, capture_limit)
        return replace(run, runtime_identity="another-runtime")


class Crash(Exception):
    """Stands for the process dying: nothing after this point runs."""


async def ready_to_integrate(world: TrunkWorld) -> tuple[str, str]:
    await world.integration.require_check(PROJECT, ZERO_STAYS_ZERO)
    base = await world.awaiting_builder()
    return base, await world.reviewed(on=base)


def held(world: TrunkWorld, inner: Isolation | None = None) -> HeldAtABarrier:
    return HeldAtABarrier(inner or ContainerIsolation(world.image))


async def integrated_events(world: TrunkWorld) -> int:
    return (await world.events()).count("behaviour.integrated")


async def test_a_request_repeated_after_the_reply_was_lost_gets_the_same_answer(
    world: TrunkWorld,
) -> None:
    _, candidate = await ready_to_integrate(world)
    first = await world.integrate(request="once")

    again = await world.integrate(request="once")

    assert again == first
    assert again.outcome is Outcome.INTEGRATED
    assert await world.column("SELECT count(*) FROM check_batches") == [1]
    assert await world.column("SELECT count(*) FROM executions WHERE purpose = 'check'") == [1]
    assert await integrated_events(world) == 1
    assert world.trunk() == candidate


async def test_a_request_repeated_while_its_checks_run_does_not_start_them_again(
    world: TrunkWorld,
) -> None:
    _, candidate = await ready_to_integrate(world)
    barrier = held(world)
    running = asyncio.create_task(world.integration_with(barrier).integrate(PROJECT, "B1", "once"))
    await held_at_the_barrier(running, barrier)

    impatient = await world.integrate(request="once")
    barrier.release.set()
    original = await running

    assert impatient.outcome is Outcome.IN_PROGRESS
    assert original.outcome is Outcome.INTEGRATED
    assert impatient.batch_id == original.batch_id
    assert await world.column("SELECT count(*) FROM check_batches") == [1]
    assert world.trunk() == candidate


async def test_two_arrivals_of_one_request_at_once_make_one_batch(world: TrunkWorld) -> None:
    _, candidate = await ready_to_integrate(world)

    receipts = await asyncio.gather(
        world.integrate(request="once"), world.integrate(request="once")
    )

    assert {receipt.outcome for receipt in receipts} <= {Outcome.INTEGRATED, Outcome.IN_PROGRESS}
    assert await world.column("SELECT count(*) FROM check_batches") == [1]
    await world.until_trunk_is(candidate)


async def test_a_failing_batch_overtaken_by_a_later_request_sends_nothing_back(
    world: TrunkWorld,
) -> None:
    _, candidate = await ready_to_integrate(world)
    barrier = held(world, Falsified(ContainerIsolation(world.image)))
    overtaken = asyncio.create_task(
        world.integration_with(barrier).integrate(PROJECT, "B1", "first")
    )
    await held_at_the_barrier(overtaken, barrier)

    later = await world.integrate(request="second")
    barrier.release.set()
    late = await overtaken

    assert later.outcome is Outcome.INTEGRATED
    assert late.outcome is Outcome.SUPERSEDED
    assert await world.column("SELECT count(*) FROM remediation_requests") == [0]
    assert await world.state() is BehaviourState.INTEGRATED
    assert world.trunk() == candidate
    assert await integrated_events(world) == 1
    assert await world.column("SELECT count(*) FROM executions WHERE purpose = 'check'") == [2]


async def test_a_check_required_while_a_batch_runs_keeps_that_batch_from_deciding(
    world: TrunkWorld,
) -> None:
    base, candidate = await ready_to_integrate(world)
    barrier = held(world)
    running = asyncio.create_task(world.integration_with(barrier).integrate(PROJECT, "B1", "first"))
    await held_at_the_barrier(running, barrier)

    await world.integration.require_check(
        PROJECT, replace(ZERO_STAYS_ZERO, contract_id="zero-still-stays-zero")
    )
    barrier.release.set()
    receipt = await running

    assert receipt.outcome is Outcome.SUPERSEDED
    assert "checks required" in receipt.reason
    assert world.trunk() == base
    assert await world.state() is BehaviourState.REVIEW
    assert await world.column("SELECT count(*) FROM integration_intents") == [0]
    assert (await world.integrate()).outcome is Outcome.INTEGRATED
    assert world.trunk() == candidate
    assert await world.column("SELECT count(*) FROM executions WHERE purpose = 'check'") == [3]


_A_NEW_BLOCKER = (
    "INSERT INTO findings (finding_id, project_id, behaviour_id, role, severity, claim, "
    "evidence, affected_files, remediation, raised_in_cycle) SELECT 'F-late', project_id, "
    "behaviour_id, 'sentinel', 4, 'c', 'e', '{}', 'r', cycle_id FROM review_cycles"
)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (_A_NEW_BLOCKER, "gate"),
        ("UPDATE review_cycles SET state = 'blocked'", "no longer the current"),
        ("UPDATE behaviours SET state = 'blocked'", "no longer the current"),
        ("UPDATE behaviours SET candidate_revision = repeat('0', 40)", "the candidate on record"),
        ("UPDATE behaviours SET red_execution_id = NULL", "the red or the candidate"),
    ],
    ids=["new blocker", "cycle stopped", "behaviour parked", "other candidate", "red dropped"],
)
async def test_authorisation_looks_again_at_everything_the_checks_took_for_granted(
    world: TrunkWorld, change: str, reason: str
) -> None:
    """No path in this slice changes these under a satisfied gate, so each is written
    straight into the database while the last check is held. What is under test is that
    the authorising transaction does not take what it saw earlier on trust."""
    base, _ = await ready_to_integrate(world)
    barrier = held(world)
    running = asyncio.create_task(world.integration_with(barrier).integrate(PROJECT, "B1", "first"))
    await held_at_the_barrier(running, barrier)

    async with world.database.connection() as connection:
        await connection.execute(change)  # type: ignore[arg-type]
    barrier.release.set()
    receipt = await running

    assert receipt.outcome is Outcome.SUPERSEDED
    assert reason in receipt.reason
    assert world.trunk() == base
    assert await world.column("SELECT count(*) FROM integration_intents") == [0]
    assert await world.column("SELECT count(*) FROM remediation_requests") == [0]
    assert (await world.events())[-1] == "integration.batch_discarded"


async def test_a_failing_batch_gives_way_to_a_later_request_still_running(
    world: TrunkWorld,
) -> None:
    _, candidate = await ready_to_integrate(world)
    first_barrier = held(world, Falsified(ContainerIsolation(world.image)))
    second_barrier = held(world)
    first = asyncio.create_task(
        world.integration_with(first_barrier).integrate(PROJECT, "B1", "first")
    )
    await held_at_the_barrier(first, first_barrier)
    second = asyncio.create_task(
        world.integration_with(second_barrier).integrate(PROJECT, "B1", "second")
    )
    await held_at_the_barrier(second, second_barrier)

    first_barrier.release.set()
    overtaken = await first
    second_barrier.release.set()

    assert overtaken.outcome is Outcome.SUPERSEDED
    assert (await second).outcome is Outcome.INTEGRATED
    assert await world.column("SELECT count(*) FROM remediation_requests") == [0]
    assert world.trunk() == candidate


async def test_a_batch_cannot_authorise_while_another_integration_is_unsettled(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = await world.awaiting_builder()
    await world.awaiting_builder("B2", GREETING)
    await world.reviewed(on=base)
    await world.reviewed("B2", {"greeting.txt": "hello\n"}, on=base)
    barrier = held(world)
    await world.integration.require_check(PROJECT, ZERO_STAYS_ZERO)
    slow = asyncio.create_task(world.integration_with(barrier).integrate(PROJECT, "B1", "first"))
    await held_at_the_barrier(slow, barrier)

    async def unreachable(*_: Any, **__: Any) -> None:
        raise RemoteUnavailable("no route to host")

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "advance", unreachable)
        assert (await world.integrate("B2")).outcome is Outcome.PENDING
    barrier.release.set()
    receipt = await slow

    assert receipt.outcome is Outcome.SUPERSEDED
    assert "another integration" in receipt.reason
    assert world.trunk() == base
    assert await world.column("SELECT behaviour_id FROM integration_intents") == ["B2"]


async def test_two_reconcilers_at_once_record_one_integration(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, candidate = await ready_to_integrate(world)

    async def dies(*_: Any, **__: Any) -> None:
        raise Crash

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "advance", dies)
        with pytest.raises(Crash):
            await world.integrate()

    await asyncio.gather(world.integration.reconcile(), world.integration.reconcile())
    await world.integration.reconcile()

    assert world.trunk() == candidate
    assert await world.state() is BehaviourState.INTEGRATED
    assert await integrated_events(world) == 1


async def test_a_settled_request_is_answered_again_without_asking_the_repository(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await ready_to_integrate(world)
    first = await world.integrate(request="once")

    async def unreachable(*_: Any, **__: Any) -> None:
        raise RemoteUnavailable("no route to host")

    monkeypatch.setattr(integration_module, "trunk_head", unreachable)

    assert await world.integrate(request="once") == first


async def test_of_two_behaviours_reviewed_on_one_trunk_the_second_to_finish_starts_again(
    world: TrunkWorld,
) -> None:
    base = await world.awaiting_builder()
    await world.awaiting_builder("B2", GREETING)
    await world.reviewed(on=base)
    second = await world.reviewed("B2", {"greeting.txt": "hello\n"}, on=base)
    barrier = held(world)
    await world.integration.require_check(PROJECT, ZERO_STAYS_ZERO)
    slow = asyncio.create_task(world.integration_with(barrier).integrate(PROJECT, "B1", "first"))
    await held_at_the_barrier(slow, barrier)

    assert (await world.integrate("B2")).outcome is Outcome.INTEGRATED
    barrier.release.set()
    overtaken = await slow
    retried = await world.integrate("B1")

    assert overtaken.outcome is Outcome.SUPERSEDED
    assert retried.outcome is Outcome.BASE_MOVED
    assert world.trunk() == second
    assert await world.state("B1") is BehaviourState.VALIDATING_RED
    assert "superseded" in await world.column(
        "SELECT state FROM review_cycles WHERE behaviour_id = 'B1'"
    )
    assert (await world.behaviours.run_red(PROJECT, "B1")).is_valid
    assert await world.state("B1") is BehaviourState.IMPLEMENTING
    rebuilt = await world.reviewed(on=second)
    assert (await world.integrate("B1")).outcome is Outcome.INTEGRATED
    assert world.trunk() == rebuilt


async def test_a_moved_trunk_that_already_does_what_was_asked_is_put_to_the_product_owner(
    world: TrunkWorld,
) -> None:
    base, _ = await ready_to_integrate(world)
    elsewhere = world.advance_trunk({"vat.py": VAT_APPLIES_STANDARD_RATE})

    receipt = await world.integrate(request="once")
    again = await world.integrate(request="once")
    verdict = await world.behaviours.run_red(PROJECT, "B1")

    assert (receipt.outcome, again) == (Outcome.BASE_MOVED, receipt)
    assert verdict.rejection is RedRejection.ALREADY_PASSES
    assert await world.state() is BehaviourState.NEEDS_DECISION
    assert world.trunk() == elsewhere != base
    assert await world.column("SELECT count(*) FROM executions WHERE purpose = 'check'") == [0]
    assert await world.column("SELECT red_execution_id FROM behaviours") == [None]
    assert "integration.red_reopened" in await world.events()


async def test_checks_seen_in_another_runtime_than_the_green_reopen_the_red(
    world: TrunkWorld,
) -> None:
    base, _ = await ready_to_integrate(world)
    elsewhere = world.integration_with(AnotherRuntime(ContainerIsolation(world.image)))

    receipt = await elsewhere.integrate(PROJECT, "B1", "once")

    assert receipt.outcome is Outcome.RUNTIME_CHANGED
    assert "runtime" in receipt.reason
    assert await elsewhere.integrate(PROJECT, "B1", "once") == receipt
    assert world.trunk() == base
    assert await world.state() is BehaviourState.VALIDATING_RED
    assert await world.column("SELECT count(*) FROM integration_intents") == [0]


async def test_a_crash_just_before_the_push_is_finished_on_restart(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, candidate = await ready_to_integrate(world)

    async def dies(*_: Any, **__: Any) -> None:
        raise Crash

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "advance", dies)
        with pytest.raises(Crash):
            await world.integrate()
    assert world.trunk() == base
    assert await world.column("SELECT state FROM integration_intents") == ["outstanding"]

    assert await world.integration.reconcile() == 1
    assert await world.integration.reconcile() == 0

    assert world.trunk() == candidate
    assert await world.state() is BehaviourState.INTEGRATED
    assert await integrated_events(world) == 1


async def test_a_crash_after_the_remote_accepted_the_push_is_recognised_on_restart(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, candidate = await ready_to_integrate(world)
    real = Mirror.advance

    async def dies_after_sending(self: Mirror, *arguments: Any, **options: Any) -> None:
        await real(self, *arguments, **options)
        raise Crash

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "advance", dies_after_sending)
        with pytest.raises(Crash):
            await world.integrate()
    assert world.trunk() == candidate
    assert await world.state() is BehaviourState.REVIEW

    assert await world.integration.reconcile() == 1

    assert await world.state() is BehaviourState.INTEGRATED
    assert await integrated_events(world) == 1
    assert await world.column("SELECT sends_started FROM integration_intents") == [1]


@pytest.mark.parametrize("how", ["built on top", "merged and discarded"])
async def test_a_later_head_that_contains_the_candidate_is_not_taken_as_its_integration(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """A commit can be in trunk's history while its change is not in trunk's tree."""
    base, candidate = await ready_to_integrate(world)

    async def dies(*_: Any, **__: Any) -> None:
        raise Crash

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "advance", dies)
        with pytest.raises(Crash):
            await world.integrate()
    if how == "built on top":
        world.git("push", "-q", "origin", f"{candidate}:refs/heads/main")
        world.advance_trunk({"notes.txt": "built on the candidate\n"})
    else:
        world.git("checkout", "-q", "--detach", base)
        world.git("merge", "-q", "--no-ff", "-s", "ours", "-m", "keep ours", candidate)
        world.git("push", "-q", "origin", "HEAD:refs/heads/main")

    assert await world.integration.reconcile() == 1

    assert await world.column("SELECT state FROM integration_intents") == ["conflicted"]
    assert await world.state() is BehaviourState.VALIDATING_RED
    assert await integrated_events(world) == 0


async def test_trunk_found_elsewhere_after_a_lost_push_sends_the_behaviour_for_a_fresh_red(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await ready_to_integrate(world)
    await world.awaiting_builder("B2", GREETING)

    async def dies(*_: Any, **__: Any) -> None:
        raise Crash

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "advance", dies)
        with pytest.raises(Crash):
            await world.integrate()
    elsewhere = world.advance_trunk({"notes.txt": "someone else was here\n"})

    assert await world.integration.reconcile() == 1

    assert world.trunk() == elsewhere
    assert await world.state() is BehaviourState.VALIDATING_RED
    assert await world.column("SELECT state FROM integration_intents") == ["conflicted"]
    assert await world.column("SELECT state FROM review_cycles") == ["superseded"]
    assert (await world.events())[-2:] == ["integration.conflicted", "integration.red_reopened"]
    with pytest.raises(IntegrationRefused, match="no failed integration"):
        await world.integration.retry_integration(PROJECT, "B1", "not needed")
    second = await world.reviewed("B2", {"greeting.txt": "hello\n"}, on=elsewhere)
    assert second and (await world.integrate("B2")).outcome is Outcome.BASE_MOVED


async def test_a_reading_of_trunk_behind_the_expected_head_settles_nothing(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old reading does not show that a push still in flight can no longer land."""
    base, candidate = await ready_to_integrate(world)

    async def dies(*_: Any, **__: Any) -> None:
        raise Crash

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "advance", dies)
        with pytest.raises(Crash):
            await world.integrate()
    world.git("update-ref", "refs/heads/main", f"{base}~1", at=world.remote)

    assert await world.integration.reconcile() == 0

    assert await world.column("SELECT state FROM integration_intents") == ["outstanding"]
    assert await world.column("SELECT sends_started FROM integration_intents") == [0]
    assert await world.state() is BehaviourState.REVIEW
    world.git("update-ref", "refs/heads/main", base, at=world.remote)
    assert await world.integration.reconcile() == 1
    assert world.trunk() == candidate


async def test_a_repository_that_cannot_be_reached_leaves_the_intent_for_the_next_pass(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, candidate = await ready_to_integrate(world)

    async def unreachable(*_: Any, **__: Any) -> None:
        raise RemoteUnavailable("no route to host")

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "_push", unreachable)
        receipt = await world.integrate()
        patched.setattr(Mirror, "read_ref", unreachable)
        assert await world.integration.reconcile() == 0

    assert receipt.outcome is Outcome.PENDING
    assert await world.state() is BehaviourState.REVIEW
    assert await world.column("SELECT state FROM integration_intents") == ["outstanding"]
    assert "integration.push_unanswered" in await world.events()
    assert await world.column("SELECT sends_started FROM integration_intents") == [1]
    assert await world.integration.reconcile() == 1
    assert world.trunk() == candidate


async def test_trunk_moved_by_someone_else_during_the_checks_sends_the_behaviour_for_a_fresh_red(
    world: TrunkWorld,
) -> None:
    _, candidate = await ready_to_integrate(world)
    barrier = held(world)
    running = asyncio.create_task(world.integration_with(barrier).integrate(PROJECT, "B1", "first"))
    await held_at_the_barrier(running, barrier)

    elsewhere = world.advance_trunk({"notes.txt": "someone else was here\n"})
    barrier.release.set()
    receipt = await running

    assert receipt.outcome is Outcome.CONFLICTED
    assert world.trunk() == elsewhere != candidate
    assert await world.state() is BehaviourState.VALIDATING_RED
    assert await world.column("SELECT state FROM integration_intents") == ["conflicted"]
    assert await world.column("SELECT sends_started FROM integration_intents") == [1]


async def test_a_hook_that_refuses_the_push_settles_the_intent_and_frees_the_project(
    world: TrunkWorld,
) -> None:
    base = await world.awaiting_builder()
    await world.awaiting_builder("B2", GREETING)
    await world.reviewed(on=base)
    other = await world.reviewed("B2", {"greeting.txt": "hello\n"}, on=base)
    world.hook_flag("refuse").touch()

    receipt = await world.integrate()

    assert receipt.outcome is Outcome.REJECTED
    assert "pre-receive hook declined" in receipt.reason
    assert world.trunk() == base
    assert await world.state() is BehaviourState.BLOCKED
    assert (await world.events())[-1] == "integration.rejected"
    assert await world.integration.reconcile() == 0
    assert await world.column("SELECT sends_started FROM integration_intents") == [1]
    world.hook_flag("refuse").unlink()
    assert (await world.integrate("B2")).outcome is Outcome.INTEGRATED
    assert world.trunk() == other


async def test_a_refused_integration_is_tried_again_only_when_someone_says_so(
    world: TrunkWorld,
) -> None:
    _, candidate = await ready_to_integrate(world)
    with pytest.raises(IntegrationRefused, match="no failed integration"):
        await world.integration.retry_integration(PROJECT, "B1", "nothing failed yet")
    world.hook_flag("refuse").touch()
    refused = await world.integrate(request="first")
    world.hook_flag("refuse").unlink()

    assert await world.integrate(request="first") == refused
    with pytest.raises(IntegrationRefused):
        await world.integrate()
    with pytest.raises(ValueError, match="reason"):
        await world.integration.retry_integration(PROJECT, "B1", " ")
    await world.integration.retry_integration(PROJECT, "B1", "the hook was misconfigured")
    receipt = await world.integrate()

    assert receipt.outcome is Outcome.INTEGRATED
    assert world.trunk() == candidate
    assert await world.column("SELECT state FROM integration_intents ORDER BY created_at") == [
        "rejected",
        "integrated",
    ]
    assert "integration.retry_allowed" in await world.events()


async def test_a_refusal_does_not_free_the_project_while_an_earlier_push_may_still_land(
    world: TrunkWorld,
) -> None:
    base = await world.awaiting_builder()
    await world.awaiting_builder("B2", GREETING)
    candidate = await world.reviewed(on=base)
    await world.reviewed("B2", {"greeting.txt": "hello\n"}, on=base)
    impatient = world.integration_with(push_timeout=2.0)
    world.hook_flag("hold").touch()

    first = await impatient.integrate(PROJECT, "B1", "once")
    await world.until_exists(world.hook_flag("reached"))
    world.hook_flag("refuse").touch()
    assert await impatient.reconcile() == 0

    assert first.outcome is Outcome.PENDING
    assert world.trunk() == base
    assert await world.column("SELECT state FROM integration_intents") == ["outstanding"]
    assert "integration.refusal_left_unsettled" in await world.events()
    assert (await world.integrate("B2")).outcome is Outcome.BUSY
    assert await impatient.reconcile() == 0
    [intent_id] = await world.column("SELECT intent_id FROM integration_intents")
    assert (await impatient._drive(intent_id)).outcome is Outcome.PENDING  # type: ignore[arg-type]
    assert await world.column("SELECT sends_started FROM integration_intents") == [2]

    world.hook_flag("refuse").unlink()
    world.hook_flag("hold").unlink()
    await world.until_trunk_is(candidate)
    assert await impatient.reconcile() == 1

    assert await world.state() is BehaviourState.INTEGRATED
    assert await integrated_events(world) == 1
    assert await world.column("SELECT state FROM integration_intents") == ["integrated"]
    assert (await world.integrate("B2")).outcome is Outcome.BASE_MOVED


async def test_a_review_conflict_seen_after_authorisation_cannot_stop_the_integration(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, candidate = await ready_to_integrate(world)

    async def unreachable(*_: Any, **__: Any) -> None:
        raise RemoteUnavailable("no route to host")

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "advance", unreachable)
        await world.integrate()
    [cycle_id] = await world.column("SELECT cycle_id FROM review_cycles")

    await world.reviews._block(cycle_id, "the candidate ref moved")  # type: ignore[arg-type]

    assert await world.column("SELECT state FROM review_cycles") == ["satisfied"]
    assert (await world.events())[-1] == "review.conflict_observed"
    assert await world.integration.reconcile() == 1
    assert world.trunk() == candidate
    assert await world.state() is BehaviourState.INTEGRATED


async def test_checks_that_could_not_be_completed_leave_the_request_answered_and_a_new_one_free(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, candidate = await ready_to_integrate(world)

    async def mirror_is_broken(*_: Any, **__: Any) -> None:
        raise CheckoutFailed("the object store is unreadable")

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "export", mirror_is_broken)
        with pytest.raises(CheckoutFailed):
            await world.integrate(request="first")

    assert (await world.integrate(request="first")).outcome is Outcome.SUPERSEDED
    assert await world.column("SELECT state FROM check_batches") == ["abandoned"]
    assert (await world.integrate()).outcome is Outcome.INTEGRATED
    assert world.trunk() == candidate


async def test_an_integration_seen_twice_to_have_converged_is_recorded_once(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two reconcilers can both find trunk at the candidate before either records it."""
    await ready_to_integrate(world)
    real = Mirror.advance

    async def dies_after_sending(self: Mirror, *arguments: Any, **options: Any) -> None:
        await real(self, *arguments, **options)
        raise Crash

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "advance", dies_after_sending)
        with pytest.raises(Crash):
            await world.integrate()
    [intent_id] = await world.column("SELECT intent_id FROM integration_intents")

    first = await world.integration._converged(intent_id)  # type: ignore[arg-type]
    second = await world.integration._converged(intent_id)  # type: ignore[arg-type]

    assert first == second
    assert first.outcome is Outcome.INTEGRATED
    assert await integrated_events(world) == 1


@pytest.mark.parametrize("fault", ["one check missing", "run on another commit"])
async def test_runs_that_are_not_every_check_on_the_candidate_authorise_nothing(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    base, _ = await ready_to_integrate(world)
    await world.integration.require_check(
        PROJECT, replace(ZERO_STAYS_ZERO, contract_id="zero-still-stays-zero")
    )
    real = integration_module.Integration._run_checks

    async def faulty(self: Any, batch: dict[str, Any]) -> Any:
        runs = await real(self, batch)
        if fault == "one check missing":
            return runs[:1]
        return [(check, replace(execution, revision=base)) for check, execution in runs]

    monkeypatch.setattr(integration_module.Integration, "_run_checks", faulty)

    receipt = await world.integrate()

    assert receipt.outcome is Outcome.SUPERSEDED
    assert world.trunk() == base
    assert await world.column("SELECT count(*) FROM integration_intents") == [0]
    assert await world.column("SELECT state FROM check_batches") == ["discarded"]
    assert await world.state() is BehaviourState.REVIEW


async def test_a_project_with_no_checks_at_all_still_integrates(world: TrunkWorld) -> None:
    base = await world.awaiting_builder()
    candidate = await world.reviewed(on=base)

    assert (await world.integrate()).outcome is Outcome.INTEGRATED
    assert world.trunk() == candidate


async def test_an_obligation_that_arrives_twice_is_one_check_run_once(world: TrunkWorld) -> None:
    base = await world.awaiting_builder()
    await world.reviewed(on=base)
    await world.integrate()
    await world.integration.require_check(PROJECT, CONTRACT.as_check())
    trunk = await world.awaiting_builder("B2", GREETING)
    candidate = await world.reviewed("B2", {"greeting.txt": "hello\n"}, on=trunk)

    receipt = await world.integrate("B2")

    assert receipt.outcome is Outcome.INTEGRATED
    assert world.trunk() == candidate
    assert await world.column("SELECT count(*) FROM executions WHERE purpose = 'check'") == [1]


async def test_a_repository_enrolled_by_a_relative_path_is_the_one_that_moves(
    database: AsyncConnectionPool,
    tmp_path: Path,
    runner_image: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = TrunkWorld(database, tmp_path, runner_image)
    monkeypatch.chdir(tmp_path)
    base = await world.open(address="remote.git")
    elsewhere = tmp_path / "another-directory"
    elsewhere.mkdir()
    world.git("clone", "-q", "--bare", str(world.remote), str(elsewhere / "remote.git"))
    monkeypatch.chdir(elsewhere)
    await world.awaiting_builder()
    candidate = await world.reviewed(on=base)

    assert (await world.integrate()).outcome is Outcome.INTEGRATED

    assert world.trunk() == candidate
    assert world.git("rev-parse", "refs/heads/main", at=elsewhere / "remote.git") == base
    assert await world.column("SELECT repository FROM integration_intents") == [
        str(world.remote.resolve())
    ]


async def test_a_local_failure_before_anything_is_sent_is_not_a_push_to_wait_for(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, _ = await ready_to_integrate(world)

    async def unreadable(*_: Any, **__: Any) -> bool:
        raise CheckoutFailed("the object store is unreadable")

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "holds", unreadable)
        receipt = await world.integration.integrate(PROJECT, "B1", "once")
    world.hook_flag("refuse").touch()
    assert receipt.outcome is Outcome.PENDING
    assert await world.column("SELECT sends_started FROM integration_intents") == [0]
    assert "integration.push_unanswered" not in await world.events()

    assert await world.integration.reconcile() == 1

    assert world.trunk() == base
    assert await world.column("SELECT state FROM integration_intents") == ["rejected"]
    assert await world.state() is BehaviourState.BLOCKED


async def test_a_conflict_seen_under_an_intent_that_is_then_refused_stops_the_cycle_being_reused(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, _ = await ready_to_integrate(world)
    real = Mirror.advance
    world.hook_flag("refuse").touch()

    async def conflict_seen_first(self: Mirror, *arguments: Any, **options: Any) -> bool:
        [cycle_id] = await world.column("SELECT cycle_id FROM review_cycles")
        await world.reviews._block(cycle_id, "the candidate ref moved")  # type: ignore[arg-type]
        return await real(self, *arguments, **options)

    monkeypatch.setattr(Mirror, "advance", conflict_seen_first)
    receipt = await world.integrate()
    world.hook_flag("refuse").unlink()

    assert receipt.outcome is Outcome.REJECTED
    assert await world.column("SELECT state FROM review_cycles") == ["blocked"]
    assert await world.column("SELECT blocked_reason FROM review_cycles") == [
        "the candidate ref moved"
    ]
    with pytest.raises(IntegrationRefused, match="no failed integration"):
        await world.integration.retry_integration(PROJECT, "B1", "the hook was fixed")
    assert world.trunk() == base
    assert await world.column("SELECT sends_started FROM integration_intents") == [1]


async def test_an_update_found_locally_to_be_no_fast_forward_is_refused_without_a_send(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, _ = await ready_to_integrate(world)

    async def not_a_fast_forward(*_: Any, **__: Any) -> bool:
        raise NotFastForward("the commit is not a fast-forward of the expected head")

    monkeypatch.setattr(Mirror, "advance", not_a_fast_forward)

    receipt = await world.integrate()

    assert receipt.outcome is Outcome.REJECTED
    assert world.trunk() == base
    assert await world.column("SELECT sends_started + sends_resolved FROM integration_intents") == [
        0
    ]
    assert await world.state() is BehaviourState.BLOCKED


async def test_what_is_authorised_is_the_batch_on_record_not_what_the_runner_was_holding(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, candidate = await ready_to_integrate(world)
    another = world.publish_attempt("B1", {"vat.py": VAT_ALWAYS_PRINTS_120}, on=candidate)
    real = integration_module.Integration._run_checks

    async def tampering(self: Any, batch: dict[str, Any]) -> Any:
        runs = await real(self, batch)
        batch["candidate_revision"] = another
        batch["expected_trunk"] = candidate
        return runs

    monkeypatch.setattr(integration_module.Integration, "_run_checks", tampering)

    receipt = await world.integrate()

    assert receipt.outcome is Outcome.INTEGRATED
    assert world.trunk() == candidate
    assert await world.column(
        "SELECT candidate_revision, expected_trunk FROM integration_intents"
    ) == [candidate]
    assert await world.column("SELECT expected_trunk FROM integration_intents") == [base]


async def test_a_push_that_could_not_even_be_started_is_not_a_push_to_wait_for(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, _ = await ready_to_integrate(world)
    real = subprocess.Popen

    def no_git_for_a_push(command: list[str], **options: Any) -> Any:
        if "push" in command:
            raise FileNotFoundError(2, "No such file or directory", "git")
        return real(command, **options)

    with monkeypatch.context() as patched:
        patched.setattr(checkout_module.subprocess, "Popen", no_git_for_a_push)
        receipt = await world.integrate()
    world.hook_flag("refuse").touch()
    assert receipt.outcome is Outcome.PENDING
    assert await world.column("SELECT sends_started FROM integration_intents") == [1]
    assert await world.column("SELECT sends_resolved FROM integration_intents") == [1]
    assert (await world.events())[-1] == "integration.push_not_started"

    assert await world.integration.reconcile() == 1

    assert world.trunk() == base
    assert await world.column("SELECT state FROM integration_intents") == ["rejected"]


async def test_a_mirror_lost_after_authorisation_is_rebuilt_from_the_published_candidate(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, candidate = await ready_to_integrate(world)

    async def dies(*_: Any, **__: Any) -> None:
        raise Crash

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "advance", dies)
        with pytest.raises(Crash):
            await world.integrate()
    shutil.rmtree(world.mirrors)

    assert await world.integration.reconcile() == 1

    assert world.trunk() == candidate
    assert await world.state() is BehaviourState.INTEGRATED


async def test_a_send_whose_error_arrives_after_the_integration_was_settled_says_integrated(
    world: TrunkWorld,
) -> None:
    await ready_to_integrate(world)
    await world.integrate()
    [intent_id] = await world.column("SELECT intent_id FROM integration_intents")

    late = await world.integration._unanswered(intent_id, "connection reset")  # type: ignore[arg-type]

    assert late.outcome is Outcome.INTEGRATED
    assert "integration.push_unanswered" not in await world.events()


async def test_a_launch_that_may_have_run_git_keeps_the_send_unanswered(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launcher failed without proof that Git stayed put. Git may have pushed, so a
    later refusal must not free the project."""
    base, _ = await ready_to_integrate(world)
    real = subprocess.Popen

    def acknowledgement_lost_for_a_push(command: list[str], **options: Any) -> Any:
        if "push" in command:
            raise OSError(5, "Input/output error")
        return real(command, **options)

    with monkeypatch.context() as patched:
        patched.setattr(checkout_module.subprocess, "Popen", acknowledgement_lost_for_a_push)
        receipt = await world.integrate()
    world.hook_flag("refuse").touch()

    assert receipt.outcome is Outcome.PENDING
    assert await world.integration.reconcile() == 0

    assert world.trunk() == base
    assert await world.column("SELECT state FROM integration_intents") == ["outstanding"]
    assert await world.column("SELECT sends_started - sends_resolved FROM integration_intents") == [
        1
    ]
    assert "integration.push_not_started" not in await world.events()


async def test_the_last_answer_to_arrive_settles_an_intent_already_refused(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sends in flight, one refused, the other then known never to have started. The
    counters are set directly: two sends cannot be put in flight together from outside."""
    base, _ = await ready_to_integrate(world)

    async def dies(*_: Any, **__: Any) -> None:
        raise Crash

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "advance", dies)
        with pytest.raises(Crash):
            await world.integrate()
    async with world.database.connection() as connection:
        await connection.execute(
            "UPDATE integration_intents SET sends_started = 2, sends_resolved = 1, "
            "refusal = 'pre-receive hook declined'"
        )
    [intent_id] = await world.column("SELECT intent_id FROM integration_intents")

    receipt = await world.integration._send_never_left(intent_id, "no descriptors")  # type: ignore[arg-type]

    assert receipt.outcome is Outcome.REJECTED
    assert receipt.reason == "pre-receive hook declined"
    assert world.trunk() == base
    assert await world.column("SELECT state FROM integration_intents") == ["rejected"]
    assert await world.state() is BehaviourState.BLOCKED


@pytest.mark.parametrize("then", ["returns", "raises"])
async def test_which_batch_is_settled_is_not_for_the_check_runner_to_say(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch, then: str
) -> None:
    _, candidate = await ready_to_integrate(world)
    real = integration_module.Integration._run_checks

    async def renames_its_batch(self: Any, batch: dict[str, Any]) -> Any:
        runs = await real(self, batch)
        batch["batch_id"] = uuid.uuid4()
        if then == "raises":
            raise CheckoutFailed("the object store is unreadable")
        return runs

    monkeypatch.setattr(integration_module.Integration, "_run_checks", renames_its_batch)

    if then == "returns":
        assert (await world.integrate()).outcome is Outcome.INTEGRATED
        assert world.trunk() == candidate
    else:
        with pytest.raises(CheckoutFailed):
            await world.integrate()
        assert await world.column("SELECT state FROM check_batches") == ["abandoned"]


async def test_a_project_whose_candidate_cannot_be_fetched_does_not_hold_up_another(
    world: TrunkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await ready_to_integrate(world)
    other = TrunkWorld(
        world.database,
        world.tmp_path / "other",
        world.image,
        project="other",
        mirrors=world.mirrors,
    )
    (world.tmp_path / "other").mkdir()
    other_base = await other.open()
    await other.awaiting_builder()
    other_candidate = await other.reviewed(on=other_base)
    impatient = world.integration_with(push_timeout=1.0)

    async def dies(*_: Any, **__: Any) -> None:
        raise Crash

    with monkeypatch.context() as patched:
        patched.setattr(Mirror, "advance", dies)
        for each in (world, other):
            with pytest.raises(Crash):
                await each.integrate()
    [stalled_ref] = await world.column(
        f"SELECT candidate_ref FROM review_cycles WHERE project_id = '{PROJECT}'"
    )
    world.git(
        "update-ref", "-d", f"refs/relay/pins/{await pinned_candidate(world)}",
        at=world.mirrors / f"{PROJECT}.git",
    )  # fmt: skip
    real = mirror_module.git_result

    async def candidate_fetch_stalls(*arguments: str, **options: Any) -> Any:
        if "fetch" in arguments and any(str(stalled_ref) in argument for argument in arguments):
            await asyncio.sleep(options["timeout"] if options.get("timeout") else 3600)
            raise RemoteUnavailable("git fetch gave no answer in time")
        return await real(*arguments, **options)

    monkeypatch.setattr(mirror_module, "git_result", candidate_fetch_stalls)

    async with asyncio.timeout(60):
        settled = await impatient.reconcile()

    assert settled == 1
    assert other.trunk() == other_candidate
    assert await world.column(
        "SELECT state, sends_started FROM integration_intents ORDER BY created_at"
    ) == ["outstanding", "integrated"]
    assert await world.column(
        f"SELECT sends_started FROM integration_intents WHERE project_id = '{PROJECT}'"
    ) == [0]


async def pinned_candidate(world: TrunkWorld) -> str:
    [candidate] = await world.column(
        f"SELECT candidate_revision FROM integration_intents WHERE project_id = '{PROJECT}'"
    )
    return str(candidate)
