"""Scenario 7: a reviewed candidate reaches trunk only after every required check has been
observed on that exact commit, and trunk moves once, to that commit, or not at all."""

from __future__ import annotations

from pathlib import Path

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.integration import IntegrationRefused, Outcome
from relay.domain.behaviour import BehaviourState
from relay.persistence.outbox import pending_messages
from relay.testing.oracle import ContainerIsolation, NativeIsolation
from tests.integration.fixtures import VAT_APPLIES_STANDARD_RATE, CountingLaunches
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
