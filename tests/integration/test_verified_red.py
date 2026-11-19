"""Scenario 4, end to end: given a new behaviour, the Builder stays blocked until Relay
itself observes the registered baseline on a real, pinned revision of the subject."""

from __future__ import annotations

from pathlib import Path

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.behaviours import Behaviours
from relay.application.red_runs import RedRuns
from relay.domain.behaviour import BehaviourState
from relay.domain.evidence import Launch, ProcessContract, RedRejection
from relay.git.checkout import CheckoutFailed
from relay.persistence.ledger import read_events
from relay.persistence.outbox import pending_messages
from relay.testing.oracle import NativeIsolation, ProcessOracle
from tests.integration.fixtures import CONTRACT, VAT_RETURNS_NET, commit

PROJECT = "vat-calculator"


async def awaiting_red(
    database: AsyncConnectionPool,
    contract: ProcessContract = CONTRACT,
    *,
    native_evidence_approved: bool = True,
) -> tuple[Behaviours, RedRuns]:
    behaviours = Behaviours(database)
    await behaviours.open_project(PROJECT, native_evidence_approved=native_evidence_approved)
    await behaviours.propose(PROJECT, "B1", "standard VAT rate is applied")
    await behaviours.begin_test_design(PROJECT, "B1")
    await behaviours.register_acceptance_test(PROJECT, "B1", contract)
    return behaviours, RedRuns(behaviours, ProcessOracle(NativeIsolation()))


async def builder_is_blocked(behaviours: Behaviours, database: AsyncConnectionPool) -> bool:
    state = await behaviours.state_of(PROJECT, "B1")
    return state is BehaviourState.VALIDATING_RED and await pending_messages(database) == []


async def test_the_wrong_business_value_releases_the_builder(
    database: AsyncConnectionPool, tmp_path: Path
) -> None:
    baseline = commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})
    behaviours, red_runs = await awaiting_red(database)

    verdict = await red_runs.run(PROJECT, "B1", str(tmp_path / "subject"), baseline)

    assert verdict.is_valid
    assert await behaviours.state_of(PROJECT, "B1") is BehaviourState.IMPLEMENTING
    [request] = await pending_messages(database)
    assert request.envelope["event_type"] == "build.requested"
    assert request.envelope["payload"]["base_revision"] == baseline


async def test_the_evidence_is_recorded_with_what_produced_it(
    database: AsyncConnectionPool, tmp_path: Path
) -> None:
    baseline = commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})
    _, red_runs = await awaiting_red(database)

    await red_runs.run(PROJECT, "B1", str(tmp_path / "subject"), baseline)

    verified = (await read_events(database, PROJECT))[-1]
    assert verified.payload["revision"] == baseline
    assert verified.payload["contract_digest"] == CONTRACT.digest
    assert verified.payload["isolation"] == "native"
    async with database.connection() as connection:
        execution = await (await connection.execute("SELECT * FROM executions")).fetchone()
    assert execution is not None
    assert str(execution["execution_id"]) == verified.payload["execution_id"]
    assert (execution["purpose"], execution["revision"]) == ("red", baseline)
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
            Launch(argv=("no-such-interpreter", "vat.py", "100")),
            RedRejection.NOT_EXECUTED,
            id="a-subject-that-cannot-launch-proves-nothing",
        ),
        pytest.param(
            {"vat.py": "import time\ntime.sleep(60)\n"},
            Launch(argv=("python3", "vat.py", "100"), timeout_seconds=0.5),
            RedRejection.TIMED_OUT,
            id="a-hang-is-not-a-failure",
        ),
        pytest.param(
            {
                "vat.py": (
                    "import sys, pathlib\n"
                    "pathlib.Path('report.xml').write_text("
                    '\'<testsuite failures="1"><testcase><failure>assert 100 == 120'
                    "</failure></testcase></testsuite>')\n"
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
    files: dict[str, str],
    launch: Launch,
    rejection: RedRejection,
) -> None:
    baseline = commit(tmp_path / "subject", files)
    contract = ProcessContract(
        CONTRACT.contract_id, CONTRACT.observes, launch, CONTRACT.expect, CONTRACT.baseline
    )
    behaviours, red_runs = await awaiting_red(database, contract)

    verdict = await red_runs.run(PROJECT, "B1", str(tmp_path / "subject"), baseline)

    assert verdict.rejection is rejection
    assert await builder_is_blocked(behaviours, database)
    rejected = (await read_events(database, PROJECT))[-1]
    assert rejected.event_type == "behaviour.red_rejected"
    assert rejected.payload["rejection"] == rejection.value
    assert rejected.payload["revision"] == baseline


async def test_native_evidence_is_refused_until_the_project_approves_it(
    database: AsyncConnectionPool, tmp_path: Path
) -> None:
    baseline = commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})
    behaviours, red_runs = await awaiting_red(database, native_evidence_approved=False)

    verdict = await red_runs.run(PROJECT, "B1", str(tmp_path / "subject"), baseline)

    assert verdict.rejection is RedRejection.ISOLATION_NOT_APPROVED
    assert await builder_is_blocked(behaviours, database)


async def test_a_revision_that_does_not_exist_records_nothing(
    database: AsyncConnectionPool, tmp_path: Path
) -> None:
    commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})
    behaviours, red_runs = await awaiting_red(database)
    before = await read_events(database, PROJECT)

    with pytest.raises(CheckoutFailed):
        await red_runs.run(PROJECT, "B1", str(tmp_path / "subject"), "0" * 40)

    assert await read_events(database, PROJECT) == before
    assert await builder_is_blocked(behaviours, database)
