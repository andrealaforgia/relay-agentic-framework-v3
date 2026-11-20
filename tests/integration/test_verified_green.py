"""Scenario 5 (green), as far as it goes before integration exists: a Builder's candidate
is green only when Relay itself observes the expectation on it, and that observation is
paired with the red: same contract, same runtime, built on the revision the red was seen on."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.behaviours import Behaviours
from relay.domain.behaviour import BehaviourState, TransitionRefused
from relay.domain.evidence import GreenRejection, Launch, ProcessContract, RedRejection
from relay.git.checkout import CheckoutFailed
from relay.git.mirror import Mirror
from relay.persistence.ledger import read_events
from relay.persistence.outbox import pending_messages
from relay.testing.oracle import ContainerIsolation, NativeIsolation, ProcessOracle
from tests.integration.fixtures import (
    CONTRACT,
    VAT_APPLIES_STANDARD_RATE,
    VAT_RETURNS_NET,
    CountingLaunches,
    HeldAtABarrier,
    commit,
    held_at_the_barrier,
    publish_attempt,
    publish_tag_as_attempt,
)

PROJECT = "vat-calculator"


async def released_to_builder(
    database: AsyncConnectionPool, repository: Path, runner_image: str
) -> tuple[Behaviours, str]:
    """A behaviour with a verified red on trunk; returns the revision the red was observed on."""
    trunk = commit(repository, {"vat.py": VAT_RETURNS_NET})
    behaviours = Behaviours(
        database, repository.parent / "mirrors", ProcessOracle(ContainerIsolation(runner_image))
    )
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
    behaviours = Behaviours(
        database, tmp_path / "mirrors", ProcessOracle(ContainerIsolation(runner_image))
    )
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


async def executions(database: AsyncConnectionPool) -> int:
    async with database.connection() as connection:
        row = await (await connection.execute("SELECT count(*) AS n FROM executions")).fetchone()
    assert row is not None
    return int(row["n"])


async def test_an_attempt_ref_pointing_at_a_tag_never_reaches_review(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    repository = tmp_path / "subject"
    behaviours, red_revision = await released_to_builder(database, repository, runner_image)
    passing = publish_attempt(
        repository, "B1", 9, {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=red_revision
    )
    for attempt, target in ((1, red_revision), (2, passing)):
        publish_tag_as_attempt(repository, "B1", attempt, pointing_at=target)

        with pytest.raises(CheckoutFailed, match="not a commit"):
            await behaviours.run_green(PROJECT, "B1", attempt=attempt)

    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.IMPLEMENTING
    assert await executions(database) == 1


async def test_another_projects_candidate_on_the_same_repository_is_not_this_projects(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    repository = tmp_path / "subject"
    behaviours, red_revision = await released_to_builder(database, repository, runner_image)
    await behaviours.open_project("other-team", str(repository))
    await behaviours.propose("other-team", "B1", "standard VAT rate is applied")
    await behaviours.begin_test_design("other-team", "B1")
    await behaviours.register_acceptance_test("other-team", "B1", CONTRACT)
    assert (await behaviours.run_red("other-team", "B1")).is_valid
    publish_attempt(repository, "B1", 1, {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=red_revision)

    with pytest.raises(CheckoutFailed, match="attempt 1"):
        await behaviours.run_green("other-team", "B1", attempt=1)

    assert await behaviours.state_of("other-team", "B1") is BehaviourState.IMPLEMENTING
    assert (await behaviours.run_green(PROJECT, "B1", attempt=1)).is_valid


async def test_a_candidate_that_hangs_is_reported_as_a_timeout(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    repository = tmp_path / "subject"
    trunk = commit(repository, {"vat.py": VAT_RETURNS_NET})
    quick = ProcessContract(
        CONTRACT.contract_id,
        CONTRACT.observes,
        Launch(argv=CONTRACT.launch.argv, timeout_seconds=3),
        CONTRACT.expect,
        CONTRACT.baseline,
    )
    behaviours = Behaviours(
        database, tmp_path / "mirrors", ProcessOracle(ContainerIsolation(runner_image))
    )
    await behaviours.open_project(PROJECT, str(repository))
    await behaviours.propose(PROJECT, "B1", "standard VAT rate is applied")
    await behaviours.begin_test_design(PROJECT, "B1")
    await behaviours.register_acceptance_test(PROJECT, "B1", quick)
    assert (await behaviours.run_red(PROJECT, "B1")).is_valid
    publish_attempt(repository, "B1", 1, {"vat.py": "import time\ntime.sleep(60)\n"}, on=trunk)

    verdict = await behaviours.run_green(PROJECT, "B1", attempt=1)

    assert verdict.rejection is GreenRejection.TIMED_OUT
    assert (await last_event(database)).payload["rejection"] == "timed_out"


async def test_native_execution_is_refused_for_green_before_anything_is_launched(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    repository = tmp_path / "subject"
    _, red_revision = await released_to_builder(database, repository, runner_image)
    publish_attempt(repository, "B1", 1, {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=red_revision)
    native = CountingLaunches(NativeIsolation())
    on_a_native_runner = Behaviours(database, tmp_path / "mirrors", ProcessOracle(native))

    verdict = await on_a_native_runner.run_green(PROJECT, "B1", attempt=1)

    assert verdict.rejection is GreenRejection.ISOLATION_NOT_ADMISSIBLE
    assert native.launches == 0
    assert await executions(database) == 1
    refused = await last_event(database)
    assert refused.payload["rejection"] == "isolation_not_admissible"
    assert "execution_id" not in refused.payload
    assert await on_a_native_runner.state_of(PROJECT, "B1") is BehaviourState.IMPLEMENTING


async def test_with_no_red_on_record_green_is_refused_before_anything_is_launched(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    repository = tmp_path / "subject"
    _, red_revision = await released_to_builder(database, repository, runner_image)
    publish_attempt(repository, "B1", 1, {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=red_revision)
    container = CountingLaunches(ContainerIsolation(runner_image))
    behaviours = Behaviours(database, tmp_path / "mirrors", ProcessOracle(container))
    async with database.connection() as connection:
        await connection.execute("UPDATE behaviours SET red_execution_id = NULL")

    verdict = await behaviours.run_green(PROJECT, "B1", attempt=1)

    assert verdict.rejection is GreenRejection.NO_PAIRED_RED
    assert container.launches == 0
    assert await executions(database) == 1
    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.IMPLEMENTING


async def test_after_a_runtime_change_the_red_can_be_reopened_and_the_work_completed(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    repository = tmp_path / "subject"
    behaviours, red_revision = await released_to_builder(database, repository, runner_image)
    publish_attempt(repository, "B1", 1, {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=red_revision)
    async with database.connection() as connection:
        await connection.execute("UPDATE executions SET runtime_identity = 'sha256:an-older-image'")
    stuck = await behaviours.run_green(PROJECT, "B1", attempt=1)
    assert stuck.rejection is GreenRejection.RUNTIME_CHANGED

    await behaviours.reopen_red(PROJECT, "B1", reason="runner image upgraded")

    reopened = await last_event(database)
    assert reopened.event_type == "behaviour.red_reopened"
    assert reopened.payload["reason"] == "runner image upgraded"
    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.VALIDATING_RED
    async with database.connection() as connection:
        row = await (await connection.execute("SELECT red_execution_id FROM behaviours")).fetchone()
    assert row == {"red_execution_id": None}, "the superseded red must no longer be paired"
    assert (await behaviours.run_red(PROJECT, "B1")).is_valid
    assert (await behaviours.run_green(PROJECT, "B1", attempt=1)).is_valid
    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.REVIEW


async def test_reopening_a_red_needs_a_reason(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    behaviours, _ = await released_to_builder(database, tmp_path / "subject", runner_image)

    with pytest.raises(ValueError, match="reason"):
        await behaviours.reopen_red(PROJECT, "B1", reason="  ")

    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.IMPLEMENTING


async def test_a_green_in_flight_when_its_red_is_superseded_does_not_reach_review(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    repository = tmp_path / "subject"
    behaviours, first_red = await released_to_builder(database, repository, runner_image)
    publish_attempt(repository, "B1", 1, {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=first_red)
    held = HeldAtABarrier(ContainerIsolation(runner_image))
    in_flight = asyncio.create_task(
        Behaviours(database, tmp_path / "mirrors", ProcessOracle(held)).run_green(
            PROJECT, "B1", attempt=1
        )
    )
    await held_at_the_barrier(in_flight, held)

    await behaviours.reopen_red(PROJECT, "B1", reason="trunk moved")
    second_red = commit(repository, {"README.md": "trunk moved on; VAT still wrong"})
    assert (await behaviours.run_red(PROJECT, "B1")).is_valid
    held.release.set()
    verdict = await in_flight

    assert verdict.rejection is GreenRejection.RED_SUPERSEDED
    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.IMPLEMENTING
    async with database.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT e.revision, b.candidate_revision FROM behaviours b "
                "JOIN executions e ON e.execution_id = b.red_execution_id"
            )
        ).fetchone()
    assert row == {"revision": second_red, "candidate_revision": None}


async def test_a_red_in_flight_when_the_red_is_reopened_cannot_fill_the_new_cycle(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    repository = tmp_path / "subject"
    old_trunk = commit(repository, {"vat.py": VAT_RETURNS_NET})
    behaviours = Behaviours(
        database, tmp_path / "mirrors", ProcessOracle(ContainerIsolation(runner_image))
    )
    await behaviours.open_project(PROJECT, str(repository))
    await behaviours.propose(PROJECT, "B1", "standard VAT rate is applied")
    await behaviours.begin_test_design(PROJECT, "B1")
    await behaviours.register_acceptance_test(PROJECT, "B1", CONTRACT)
    held = HeldAtABarrier(ContainerIsolation(runner_image))
    late = asyncio.create_task(
        Behaviours(database, tmp_path / "mirrors", ProcessOracle(held)).run_red(PROJECT, "B1")
    )
    await held_at_the_barrier(late, held)

    assert (await behaviours.run_red(PROJECT, "B1")).is_valid
    await behaviours.reopen_red(PROJECT, "B1", reason="trunk moved")
    commit(repository, {"README.md": "trunk moved on"})
    held.release.set()
    verdict = await late

    assert verdict.rejection is RedRejection.SUPERSEDED
    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.VALIDATING_RED
    superseded = await last_event(database)
    assert superseded.payload["rejection"] == "superseded"
    assert superseded.payload["revision"] == old_trunk
    async with database.connection() as connection:
        row = await (await connection.execute("SELECT red_execution_id FROM behaviours")).fetchone()
    assert row == {"red_execution_id": None}


async def test_a_fresh_red_issues_a_build_request_with_a_new_idempotency_key(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    behaviours, _ = await released_to_builder(database, tmp_path / "subject", runner_image)
    await behaviours.reopen_red(PROJECT, "B1", reason="runner image upgraded")
    assert (await behaviours.run_red(PROJECT, "B1")).is_valid

    first, second = await pending_messages(database)

    assert first.envelope["event_type"] == second.envelope["event_type"] == "build.requested"
    assert first.envelope["idempotency_key"] != second.envelope["idempotency_key"]


async def execution_named_by(database: AsyncConnectionPool, event: Any) -> dict[str, Any]:
    async with database.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT purpose, revision FROM executions WHERE execution_id = %s",
                (event.payload["execution_id"],),
            )
        ).fetchone()
    assert row is not None, "the rejection must reference a stored execution"
    return row


async def test_a_green_released_after_reopening_but_before_the_new_red_is_still_recorded(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    repository = tmp_path / "subject"
    behaviours, first_red = await released_to_builder(database, repository, runner_image)
    candidate = publish_attempt(
        repository, "B1", 1, {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=first_red
    )
    held = HeldAtABarrier(ContainerIsolation(runner_image))
    in_flight = asyncio.create_task(
        Behaviours(database, tmp_path / "mirrors", ProcessOracle(held)).run_green(
            PROJECT, "B1", attempt=1
        )
    )
    await held_at_the_barrier(in_flight, held)

    await behaviours.reopen_red(PROJECT, "B1", reason="trunk moved")
    held.release.set()
    verdict = await in_flight

    assert verdict.rejection is GreenRejection.RED_SUPERSEDED
    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.VALIDATING_RED
    rejected = await last_event(database)
    assert rejected.event_type == "behaviour.green_rejected"
    assert rejected.payload["rejection"] == "red_superseded"
    assert await execution_named_by(database, rejected) == {
        "purpose": "green",
        "revision": candidate,
    }


async def test_a_red_released_after_the_replacement_red_was_accepted_is_still_recorded(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    repository = tmp_path / "subject"
    old_trunk = commit(repository, {"vat.py": VAT_RETURNS_NET})
    behaviours = Behaviours(
        database, tmp_path / "mirrors", ProcessOracle(ContainerIsolation(runner_image))
    )
    await behaviours.open_project(PROJECT, str(repository))
    await behaviours.propose(PROJECT, "B1", "standard VAT rate is applied")
    await behaviours.begin_test_design(PROJECT, "B1")
    await behaviours.register_acceptance_test(PROJECT, "B1", CONTRACT)
    held = HeldAtABarrier(ContainerIsolation(runner_image))
    late = asyncio.create_task(
        Behaviours(database, tmp_path / "mirrors", ProcessOracle(held)).run_red(PROJECT, "B1")
    )
    await held_at_the_barrier(late, held)

    assert (await behaviours.run_red(PROJECT, "B1")).is_valid
    await behaviours.reopen_red(PROJECT, "B1", reason="trunk moved")
    new_trunk = commit(repository, {"README.md": "trunk moved on"})
    assert (await behaviours.run_red(PROJECT, "B1")).is_valid
    held.release.set()
    verdict = await late

    assert verdict.rejection is RedRejection.SUPERSEDED
    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.IMPLEMENTING
    rejected = await last_event(database)
    assert rejected.event_type == "behaviour.red_rejected"
    assert rejected.payload["rejection"] == "superseded"
    assert await execution_named_by(database, rejected) == {"purpose": "red", "revision": old_trunk}
    async with database.connection() as connection:
        paired = await (
            await connection.execute(
                "SELECT e.revision FROM behaviours b "
                "JOIN executions e ON e.execution_id = b.red_execution_id"
            )
        ).fetchone()
    assert paired == {"revision": new_trunk}
    assert len(await pending_messages(database)) == 2


async def test_a_candidate_judged_green_stays_pinned_after_the_builder_moves_the_attempt(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    repository = tmp_path / "subject"
    behaviours, red_revision = await released_to_builder(database, repository, runner_image)
    candidate = publish_attempt(
        repository, "B1", 1, {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=red_revision
    )
    assert (await behaviours.run_green(PROJECT, "B1", attempt=1)).is_valid

    publish_attempt(repository, "B1", 1, {"vat.py": "print('moved')\n"}, on=red_revision)

    mirror = Mirror(tmp_path / "mirrors" / f"{PROJECT}.git")
    assert await mirror.holds(candidate)
    exported = await mirror.export(candidate, tmp_path / "exported")
    assert (exported / "vat.py").read_text() == VAT_APPLIES_STANDARD_RATE
