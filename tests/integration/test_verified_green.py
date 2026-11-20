"""Scenario 5 (green), as far as it goes before integration exists: a Builder's candidate
is green only when Relay itself observes the expectation on it, and that observation is
paired with the red: same contract, same runtime, built on the revision the red was seen on."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.behaviours import Behaviours
from relay.domain.behaviour import BehaviourState, TransitionRefused
from relay.domain.evidence import GreenRejection
from relay.git.checkout import CheckoutFailed
from relay.persistence.ledger import read_events
from relay.testing.oracle import ContainerIsolation, ProcessOracle
from tests.integration.fixtures import (
    CONTRACT,
    VAT_APPLIES_STANDARD_RATE,
    VAT_RETURNS_NET,
    commit,
    publish_attempt,
)

PROJECT = "vat-calculator"


async def released_to_builder(
    database: AsyncConnectionPool, repository: Path, runner_image: str
) -> tuple[Behaviours, str]:
    """A behaviour with a verified red on trunk; returns the revision the red was observed on."""
    trunk = commit(repository, {"vat.py": VAT_RETURNS_NET})
    behaviours = Behaviours(database, ProcessOracle(ContainerIsolation(runner_image)))
    await behaviours.open_project(PROJECT, str(repository))
    await behaviours.propose(PROJECT, "B1", "standard VAT rate is applied")
    await behaviours.begin_test_design(PROJECT, "B1")
    await behaviours.register_acceptance_test(PROJECT, "B1", CONTRACT)
    assert (await behaviours.run_red(PROJECT, "B1")).is_valid
    return behaviours, trunk


async def last_event(database: AsyncConnectionPool) -> Any:
    return (await read_events(database, PROJECT))[-1]


async def test_a_candidate_that_meets_the_expectation_goes_to_review_with_its_evidence(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    repository = tmp_path / "subject"
    behaviours, red_revision = await released_to_builder(database, repository, runner_image)
    candidate = publish_attempt(
        repository, "B1", 1, {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=red_revision
    )

    verdict = await behaviours.run_green(PROJECT, "B1", attempt=1)

    assert verdict.is_valid
    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.REVIEW
    verified = await last_event(database)
    assert verified.event_type == "behaviour.green_verified"
    assert verified.payload["candidate_revision"] == candidate
    assert verified.payload["red_revision"] == red_revision
    assert verified.payload["contract_digest"] == CONTRACT.digest
    async with database.connection() as connection:
        rows = await (
            await connection.execute(
                "SELECT purpose, revision, runtime_identity, stdout FROM executions "
                "ORDER BY recorded_at"
            )
        ).fetchall()
    assert [(row["purpose"], row["revision"]) for row in rows] == [
        ("red", red_revision),
        ("green", candidate),
    ]
    assert rows[0]["runtime_identity"] == rows[1]["runtime_identity"]
    assert bytes(rows[1]["stdout"]) == b"120\n"


async def test_a_candidate_that_still_fails_stays_with_the_builder_who_may_try_again(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    repository = tmp_path / "subject"
    behaviours, red_revision = await released_to_builder(database, repository, runner_image)
    publish_attempt(repository, "B1", 1, {"vat.py": "print(121)\n"}, on=red_revision)
    publish_attempt(repository, "B1", 2, {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=red_revision)

    first = await behaviours.run_green(PROJECT, "B1", attempt=1)

    assert first.rejection is GreenRejection.EXPECTATION_NOT_MET
    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.IMPLEMENTING
    rejected = await last_event(database)
    assert rejected.event_type == "behaviour.green_rejected"
    assert rejected.payload["rejection"] == "expectation_not_met"
    assert rejected.payload["attempt"] == 1

    assert (await behaviours.run_green(PROJECT, "B1", attempt=2)).is_valid


async def test_a_candidate_that_claims_success_without_delivering_it_is_not_green(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    repository = tmp_path / "subject"
    behaviours, red_revision = await released_to_builder(database, repository, runner_image)
    boasts = (
        "import sys\n"
        "print('1 passed in 0.01s', file=sys.stderr)\n"
        "print('<testsuite tests=\"1\" failures=\"0\"/>', file=open('/tmp/report.xml', 'w'))\n"
        "print(int(sys.argv[1]))\n"
    )
    publish_attempt(repository, "B1", 1, {"vat.py": boasts}, on=red_revision)

    verdict = await behaviours.run_green(PROJECT, "B1", attempt=1)

    assert verdict.rejection is GreenRejection.EXPECTATION_NOT_MET
    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.IMPLEMENTING


async def test_a_candidate_not_built_on_the_revision_the_red_was_seen_on_is_refused(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    repository = tmp_path / "subject"
    unrelated_past = commit(repository, {"vat.py": VAT_APPLIES_STANDARD_RATE})
    behaviours, _ = await released_to_builder(database, repository, runner_image)
    publish_attempt(
        repository, "B1", 1, {"note.txt": "passes, but is older than the red"}, on=unrelated_past
    )

    verdict = await behaviours.run_green(PROJECT, "B1", attempt=1)

    assert verdict.rejection is GreenRejection.NOT_BUILT_ON_THE_RED_REVISION
    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.IMPLEMENTING


async def test_green_in_a_different_runtime_from_the_red_is_refused(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    repository = tmp_path / "subject"
    behaviours, red_revision = await released_to_builder(database, repository, runner_image)
    publish_attempt(repository, "B1", 1, {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=red_revision)
    async with database.connection() as connection:
        await connection.execute("UPDATE executions SET runtime_identity = 'sha256:an-older-image'")

    verdict = await behaviours.run_green(PROJECT, "B1", attempt=1)

    assert verdict.rejection is GreenRejection.RUNTIME_CHANGED
    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.IMPLEMENTING


async def test_an_attempt_that_was_never_published_is_on_the_ledger(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    behaviours, _ = await released_to_builder(database, tmp_path / "subject", runner_image)

    with pytest.raises(CheckoutFailed, match="attempt 7"):
        await behaviours.run_green(PROJECT, "B1", attempt=7)

    failed = await last_event(database)
    assert failed.event_type == "behaviour.green_preparation_failed"
    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.IMPLEMENTING


async def test_green_cannot_be_run_before_red(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    repository = tmp_path / "subject"
    trunk = commit(repository, {"vat.py": VAT_RETURNS_NET})
    behaviours = Behaviours(database, ProcessOracle(ContainerIsolation(runner_image)))
    await behaviours.open_project(PROJECT, str(repository))
    await behaviours.propose(PROJECT, "B1", "standard VAT rate is applied")
    await behaviours.begin_test_design(PROJECT, "B1")
    await behaviours.register_acceptance_test(PROJECT, "B1", CONTRACT)
    publish_attempt(repository, "B1", 1, {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=trunk)

    with pytest.raises(TransitionRefused):
        await behaviours.run_green(PROJECT, "B1", attempt=1)

    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.VALIDATING_RED


async def test_a_caller_cannot_supply_the_candidate_or_the_observation(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    repository = tmp_path / "subject"
    behaviours, red_revision = await released_to_builder(database, repository, runner_image)
    before = await read_events(database, PROJECT)
    run_green: Any = behaviours.run_green

    for supplied in (
        {"revision": red_revision},
        {"candidate": "c" * 40},
        {"observation": b"120\n"},
    ):
        with pytest.raises(TypeError):
            await run_green(PROJECT, "B1", 1, **supplied)
    for not_an_attempt in ("refs/heads/main", "../../heads/main", 0, -1, True):
        with pytest.raises(CheckoutFailed):
            await run_green(PROJECT, "B1", not_an_attempt)

    assert [event.event_type for event in await read_events(database, PROJECT)][: len(before)] == [
        event.event_type for event in before
    ]
    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.IMPLEMENTING
