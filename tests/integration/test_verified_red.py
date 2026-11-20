"""Scenario 4, end to end: given a new behaviour, the Builder stays blocked until Relay
itself observes the registered baseline on the project's own trunk. The caller supplies
neither the source nor the observation."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.behaviours import Behaviours
from relay.domain.behaviour import BehaviourState, TransitionRefused
from relay.domain.evidence import (
    Execution,
    Launch,
    ProcessContract,
    ProcessObservation,
    RedRejection,
)
from relay.git.checkout import CheckoutFailed
from relay.persistence.ledger import read_events
from relay.persistence.outbox import pending_messages
from relay.testing.oracle import (
    ContainerIsolation,
    Isolation,
    NativeIsolation,
    ProcessOracle,
    Run,
)
from tests.integration.fixtures import (
    CONTRACT,
    VAT_APPLIES_STANDARD_RATE,
    VAT_RETURNS_NET,
    commit,
)

PROJECT = "vat-calculator"


class CountingLaunches:
    """Wraps an isolation and counts how often a subject was actually launched."""

    def __init__(self, isolation: Isolation) -> None:
        self._isolation = isolation
        self.name = isolation.name
        self.launches = 0

    async def run(self, workdir: Path, launch: Launch, capture_limit: int) -> Run:
        self.launches += 1
        return await self._isolation.run(workdir, launch, capture_limit)


def resolved_image_id(image: str) -> str:
    """The platform-specific image this host actually runs for a (possibly multi-platform) digest."""
    return subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


async def awaiting_red(
    database: AsyncConnectionPool,
    repository: Path,
    isolation: Isolation,
    contract: ProcessContract = CONTRACT,
) -> Behaviours:
    behaviours = Behaviours(database, ProcessOracle(isolation))
    await behaviours.open_project(PROJECT, str(repository))
    await behaviours.propose(PROJECT, "B1", "standard VAT rate is applied")
    await behaviours.begin_test_design(PROJECT, "B1")
    await behaviours.register_acceptance_test(PROJECT, "B1", contract)
    return behaviours


async def builder_is_blocked(behaviours: Behaviours, database: AsyncConnectionPool) -> bool:
    state = await behaviours.state_of(PROJECT, "B1")
    return state is BehaviourState.VALIDATING_RED and await pending_messages(database) == []


async def test_the_wrong_business_value_releases_exactly_one_build_request(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    trunk = commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})
    behaviours = await awaiting_red(
        database, tmp_path / "subject", ContainerIsolation(runner_image)
    )

    verdict = await behaviours.run_red(PROJECT, "B1")

    assert verdict.is_valid
    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.IMPLEMENTING
    verified = (await read_events(database, PROJECT))[-1]
    assert verified.event_type == "behaviour.test_red_verified"
    [request] = await pending_messages(database)
    assert request.stream == f"relay:{PROJECT}:cmd:builder"
    assert request.envelope["event_type"] == "build.requested"
    assert request.envelope["behaviour_id"] == "B1"
    assert request.envelope["payload"]["base_revision"] == trunk
    assert request.envelope["causation_id"] == str(verified.event_id)


async def test_the_evidence_is_recorded_with_what_produced_it(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    trunk = commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})
    behaviours = await awaiting_red(
        database, tmp_path / "subject", ContainerIsolation(runner_image)
    )

    await behaviours.run_red(PROJECT, "B1")

    verified = (await read_events(database, PROJECT))[-1]
    assert verified.payload["revision"] == trunk
    assert verified.payload["contract_digest"] == CONTRACT.digest
    assert verified.payload["isolation"] == "container"
    assert verified.payload["runtime_identity"] == resolved_image_id(runner_image)
    async with database.connection() as connection:
        execution = await (await connection.execute("SELECT * FROM executions")).fetchone()
    assert execution is not None
    assert str(execution["execution_id"]) == verified.payload["execution_id"]
    assert (execution["purpose"], execution["revision"]) == ("red", trunk)
    assert execution["runtime_identity"] == resolved_image_id(runner_image)
    assert (execution["exit_code"], bytes(execution["stdout"])) == (0, b"100\n")


@pytest.mark.parametrize(
    ("files", "launch", "rejection"),
    [
        pytest.param(
            {"vat.py": "import sys\nprint(int(sys.argv[1])\n"},
            CONTRACT.launch,
            RedRejection.UNEXPECTED_FAILURE,
            id="a-syntax-error-is-not-the-unmet-behaviour",
        ),
        pytest.param(
            {"vat.py": VAT_RETURNS_NET},
            Launch(argv=("no-such-interpreter", "vat.py", "100"), timeout_seconds=30),
            RedRejection.NOT_EXECUTED,
            id="a-subject-that-cannot-launch-proves-nothing",
        ),
        pytest.param(
            {"vat.py": "import time\ntime.sleep(60)\n"},
            Launch(argv=("python3", "vat.py", "100"), timeout_seconds=3),
            RedRejection.TIMED_OUT,
            id="a-hang-is-not-a-failure",
        ),
        pytest.param(
            {
                "vat.py": (
                    "import sys\n"
                    'print(\'<testsuite failures="1"><testcase><failure>assert 100 == 120'
                    "</failure></testcase></testsuite>', file=open('/tmp/report.xml', 'w'))\n"
                    "print('FAILED test_standard_rate - assert 100 == 120', file=sys.stderr)\n"
                    "print(int(sys.argv[1]) * 120 // 100)\n"
                ),
            },
            CONTRACT.launch,
            RedRejection.ALREADY_PASSES,
            id="a-forged-failure-report-does-not-make-a-passing-subject-red",
        ),
    ],
)
async def test_anything_else_keeps_the_builder_blocked_and_says_why(
    database: AsyncConnectionPool,
    tmp_path: Path,
    runner_image: str,
    files: dict[str, str],
    launch: Launch,
    rejection: RedRejection,
) -> None:
    trunk = commit(tmp_path / "subject", files)
    contract = ProcessContract(
        CONTRACT.contract_id, CONTRACT.observes, launch, CONTRACT.expect, CONTRACT.baseline
    )
    behaviours = await awaiting_red(
        database, tmp_path / "subject", ContainerIsolation(runner_image), contract
    )

    verdict = await behaviours.run_red(PROJECT, "B1")

    assert verdict.rejection is rejection
    assert await builder_is_blocked(behaviours, database)
    rejected = (await read_events(database, PROJECT))[-1]
    assert rejected.event_type == "behaviour.red_rejected"
    assert rejected.payload["rejection"] == rejection.value
    assert rejected.payload["revision"] == trunk


async def test_an_older_commit_cannot_be_substituted_for_a_trunk_that_already_passes(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})
    trunk = commit(tmp_path / "subject", {"vat.py": VAT_APPLIES_STANDARD_RATE})
    behaviours = await awaiting_red(
        database, tmp_path / "subject", ContainerIsolation(runner_image)
    )

    verdict = await behaviours.run_red(PROJECT, "B1")

    assert verdict.rejection is RedRejection.ALREADY_PASSES
    assert (await read_events(database, PROJECT))[-1].payload["revision"] == trunk
    assert await builder_is_blocked(behaviours, database)


async def test_native_execution_is_refused_before_anything_is_launched(
    database: AsyncConnectionPool, tmp_path: Path
) -> None:
    commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})
    native = CountingLaunches(NativeIsolation())
    behaviours = await awaiting_red(database, tmp_path / "subject", native)

    verdict = await behaviours.run_red(PROJECT, "B1")

    assert verdict.rejection is RedRejection.ISOLATION_NOT_ADMISSIBLE
    assert native.launches == 0
    assert await builder_is_blocked(behaviours, database)
    refused = (await read_events(database, PROJECT))[-1]
    assert refused.payload["rejection"] == "isolation_not_admissible"
    assert "execution_id" not in refused.payload
    async with database.connection() as connection:
        assert await (await connection.execute("SELECT 1 FROM executions")).fetchone() is None


async def test_a_released_behaviour_is_not_run_again(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})
    container = CountingLaunches(ContainerIsolation(runner_image))
    behaviours = await awaiting_red(database, tmp_path / "subject", container)
    await behaviours.run_red(PROJECT, "B1")
    before = await read_events(database, PROJECT)

    with pytest.raises(TransitionRefused):
        await behaviours.run_red(PROJECT, "B1")

    assert container.launches == 1
    assert await read_events(database, PROJECT) == before
    assert len(await pending_messages(database)) == 1


async def test_a_failed_checkout_is_on_the_ledger_and_the_builder_stays_blocked(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})
    container = CountingLaunches(ContainerIsolation(runner_image))
    behaviours = Behaviours(database, ProcessOracle(container))
    await behaviours.open_project(PROJECT, str(tmp_path / "subject"), trunk="no-such-branch")
    await behaviours.propose(PROJECT, "B1", "standard VAT rate is applied")
    await behaviours.begin_test_design(PROJECT, "B1")
    await behaviours.register_acceptance_test(PROJECT, "B1", CONTRACT)

    with pytest.raises(CheckoutFailed):
        await behaviours.run_red(PROJECT, "B1")

    assert container.launches == 0
    failed = (await read_events(database, PROJECT))[-1]
    assert failed.event_type == "behaviour.red_preparation_failed"
    assert "no-such-branch" in failed.payload["reason"]
    assert await builder_is_blocked(behaviours, database)


async def test_the_ledger_is_gap_free_and_in_order(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    commit(tmp_path / "subject", {"vat.py": "import sys\nsys.exit(3)\n"})
    behaviours = await awaiting_red(
        database, tmp_path / "subject", ContainerIsolation(runner_image)
    )
    await behaviours.run_red(PROJECT, "B1")
    commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})
    await behaviours.run_red(PROJECT, "B1")

    events = await read_events(database, PROJECT)

    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert [event.event_type for event in events] == [
        "project.opened",
        "behaviour.proposed",
        "behaviour.test_design_started",
        "behaviour.acceptance_test_registered",
        "behaviour.red_rejected",
        "behaviour.test_red_verified",
    ]


async def test_state_ledger_and_outbox_commit_together_or_not_at_all(
    database: AsyncConnectionPool,
    tmp_path: Path,
    runner_image: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})
    behaviours = await awaiting_red(
        database, tmp_path / "subject", ContainerIsolation(runner_image)
    )
    before = await read_events(database, PROJECT)

    async def crash(*_: object, **__: object) -> None:
        raise RuntimeError("crash while writing the outbox")

    monkeypatch.setattr("relay.application.behaviours.enqueue", crash)
    with pytest.raises(RuntimeError):
        await behaviours.run_red(PROJECT, "B1")

    assert await read_events(database, PROJECT) == before
    assert await builder_is_blocked(behaviours, database)
    async with database.connection() as connection:
        assert await (await connection.execute("SELECT 1 FROM executions")).fetchone() is None


async def test_the_ledger_cannot_be_rewritten(
    database: AsyncConnectionPool, tmp_path: Path
) -> None:
    await awaiting_red(database, tmp_path / "subject", NativeIsolation())

    async with database.connection() as connection:
        with pytest.raises(Exception, match="append-only"):
            await connection.execute("UPDATE events SET event_type = 'forged'")
    async with database.connection() as connection:
        with pytest.raises(Exception, match="append-only"):
            await connection.execute("DELETE FROM events")


async def test_a_caller_cannot_supply_the_observation_or_the_source(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    commit(tmp_path / "subject", {"vat.py": VAT_APPLIES_STANDARD_RATE})
    behaviours = await awaiting_red(
        database, tmp_path / "subject", ContainerIsolation(runner_image)
    )
    before = await read_events(database, PROJECT)
    forged = ProcessObservation(exit_code=0, stdout=b"100\n")
    run_red: Any = behaviours.run_red

    for supplied in (
        {"observation": forged},
        {"execution": Execution("a" * 40, "container", "image", forged)},
        {"revision": "a" * 40},
        {"repository": str(tmp_path / "elsewhere")},
    ):
        with pytest.raises(TypeError):
            await run_red(PROJECT, "B1", **supplied)
    with pytest.raises(TypeError):
        await run_red(PROJECT, "B1", forged)

    assert await read_events(database, PROJECT) == before
    assert await builder_is_blocked(behaviours, database)


@pytest.mark.parametrize(
    "address",
    [
        "https://relay:canary-secret@git.example/vat.git",
        "https://canary-secret@git.example/vat.git",
        "https://git.example/vat.git?access_token=canary-secret",
        "https://git.example/vat.git#canary-secret",
        "ssh://git:canary-secret@git.example/vat.git",
        "https://[canary-secret/vat.git",
        "https://git.example:canary-secret/vat.git",
    ],
)
async def test_a_repository_address_that_may_carry_a_credential_is_refused_and_stored_nowhere(
    database: AsyncConnectionPool, address: str
) -> None:
    behaviours = Behaviours(database, ProcessOracle(NativeIsolation()))

    with pytest.raises(ValueError, match="repository address") as refusal:
        await behaviours.open_project(PROJECT, address)

    assert "canary-secret" not in str(refusal.value)
    async with database.connection() as connection:
        assert await (await connection.execute("SELECT 1 FROM projects")).fetchone() is None
        assert await (await connection.execute("SELECT 1 FROM events")).fetchone() is None


@pytest.mark.parametrize(
    "address", ["/srv/git/vat.git", "git@git.example:team/vat.git", "ssh://git@git.example/vat.git"]
)
async def test_an_ordinary_repository_address_is_accepted(
    database: AsyncConnectionPool, address: str
) -> None:
    await Behaviours(database, ProcessOracle(NativeIsolation())).open_project(PROJECT, address)


async def test_a_failure_does_not_copy_the_repository_address_into_the_ledger(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> None:
    missing = tmp_path / "canary-address-of-a-repository-that-is-not-there"
    behaviours = await awaiting_red(database, missing, ContainerIsolation(runner_image))

    with pytest.raises(CheckoutFailed) as failure:
        await behaviours.run_red(PROJECT, "B1")

    failed = (await read_events(database, PROJECT))[-1]
    assert failed.event_type == "behaviour.red_preparation_failed"
    assert "canary-address" not in failed.payload["reason"]
    assert "canary-address" not in str(failure.value)
