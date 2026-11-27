"""A run of the boundary suite is paid for, and is on record before it is launched."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.boundaries import Boundaries
from relay.application.boundary_runs import BoundaryRuns, RunRefused
from relay.application.workers import _lock_key
from relay.providers.boundary_suite import Observation, SuiteResult

RUNTIME = {"cli": "9.9.9 (stub)", "os": "test", "remote_policy": "none, says the operator"}
DIGEST = "d" * 64


class Suite:
    """Stands in for the paid run, and counts how many were launched."""

    def __init__(self) -> None:
        self.launched = 0
        self.holds = True
        self.hold: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.fails_with: Exception | None = None

    async def __call__(self, role: str, runtime: Mapping[str, str]) -> SuiteResult:
        self.launched += 1
        self.started.set()
        if self.hold is not None and self.launched == 1:
            # Only the first is held: a second that should never have been launched
            # returns, and the test that forbids it fails there and then.
            await self.hold.wait()
        if self.fails_with is not None:
            raise self.fails_with
        return SuiteResult(
            role, DIGEST, dict(runtime), (Observation("writes stay inside", self.holds, ""),), None
        )


@pytest.fixture
def suite() -> Suite:
    return Suite()


@pytest.fixture
def runs(database: AsyncConnectionPool, suite: Suite) -> BoundaryRuns:
    return BoundaryRuns(database, Boundaries(database), suite)


async def test_a_run_is_made_once_for_a_request_however_often_the_request_is_sent(
    database: AsyncConnectionPool, runs: BoundaryRuns, suite: Suite
) -> None:
    first = await runs.run("request-1", "qa", DIGEST, RUNTIME)
    again = await runs.run("request-1", "qa", DIGEST, RUNTIME)

    assert (first.state, first.launched_now) == ("passed", True)
    assert (again.state, again.launched_now, again.attempt_id) == (
        "passed",
        False,
        first.attempt_id,
    )
    assert suite.launched == 1
    assert await Boundaries(database).holds(DIGEST)  # the result is the attempt's own


async def test_a_run_that_showed_the_bounds_not_to_hold_is_a_result_and_says_which(
    database: AsyncConnectionPool, runs: BoundaryRuns, suite: Suite
) -> None:
    suite.holds = False

    made = await runs.run("request-1", "qa", DIGEST, RUNTIME)

    assert (made.state, made.detail) == ("failed", "writes stay inside")
    assert not await Boundaries(database).holds(DIGEST)
    assert (await runs.run("request-1", "qa", DIGEST, RUNTIME)).state == "failed"
    assert suite.launched == 1


async def end_the_process_of(database: AsyncConnectionPool, attempt: uuid.UUID) -> None:
    """As the process that launched the attempt dying would: its session is gone."""
    key = _lock_key(attempt) & 0xFFFFFFFFFFFFFFFF
    async with database.connection() as connection:
        ended = await (
            await connection.execute(
                "SELECT pg_terminate_backend(pid) AS ended FROM pg_locks WHERE locktype = "
                "'advisory' AND granted AND objsubid = 1 AND classid = %s::bigint::oid "
                "AND objid = %s::bigint::oid",
                (key >> 32, key & 0xFFFFFFFF),
            )
        ).fetchall()
    assert len(ended) == 1


async def test_a_running_run_is_not_an_uncertain_one_and_neither_is_bought_again_unasked(
    database: AsyncConnectionPool, runs: BoundaryRuns, suite: Suite
) -> None:
    """3C-2.5 and 3C-3.4, as Astra put it: one launch while the first is live, two at the
    end. Running while its process lives; uncertain once it does not; retried only when
    asked, by name, once, and the retry has a receipt of its own."""
    suite.hold = asyncio.Event()
    first = asyncio.create_task(runs.run("request-1", "qa", DIGEST, RUNTIME))
    await suite.started.wait()

    replayed = await runs.run("request-1", "qa", DIGEST, RUNTIME)
    assert (replayed.state, replayed.launched_now) == ("running", False)
    with pytest.raises(RunRefused, match="is being made now"):
        await runs.run("request-2", "qa", DIGEST, RUNTIME)
    with pytest.raises(RunRefused, match="running, not uncertain"):
        await runs.run("request-3", "qa", DIGEST, RUNTIME, retry=replayed.attempt_id)
    assert suite.launched == 1

    await end_the_process_of(database, replayed.attempt_id)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)

    lost = await runs.run("request-1", "qa", DIGEST, RUNTIME)
    assert (lost.state, lost.attempt_id) == ("uncertain", replayed.attempt_id)
    with pytest.raises(RunRefused, match=f"--retry {lost.attempt_id}"):
        await runs.run("request-2", "qa", DIGEST, RUNTIME)
    assert suite.launched == 1  # never repeated by itself

    suite.hold = None
    retried = await runs.run("request-4", "qa", DIGEST, RUNTIME, retry=lost.attempt_id)

    assert (retried.state, retried.launched_now) == ("passed", True)
    assert retried.attempt_id != lost.attempt_id and suite.launched == 2
    again = await runs.run("request-4", "qa", DIGEST, RUNTIME, retry=lost.attempt_id)
    assert (again.attempt_id, again.launched_now) == (retried.attempt_id, False)
    with pytest.raises(RunRefused, match="was retried already"):
        await runs.run("request-5", "qa", DIGEST, RUNTIME, retry=lost.attempt_id)
    assert suite.launched == 2
    assert (await runs.run("request-1", "qa", DIGEST, RUNTIME)).state == "uncertain"  # as it was
    # With the uncertain one answered for, a new request for these bounds is an ordinary one.
    assert (await runs.run("request-6", "qa", DIGEST, RUNTIME)).launched_now
    assert suite.launched == 3


async def test_a_run_that_ended_without_a_result_is_uncertain_and_says_how_to_retry(
    runs: BoundaryRuns, suite: Suite
) -> None:
    suite.fails_with = RuntimeError("the terminal went away")

    with pytest.raises(RunRefused, match="may have been paid for") as refused:
        await runs.run("request-1", "qa", DIGEST, RUNTIME)

    known = await runs.of_request("request-1", "qa")
    assert known is not None and known.state == "uncertain"
    assert f"--retry {known.attempt_id}" in str(refused.value)
    suite.fails_with = None
    with pytest.raises(RunRefused, match="not repeated by itself"):
        await runs.run("request-2", "qa", DIGEST, RUNTIME)
    assert suite.launched == 1


async def test_a_retry_names_an_attempt_there_is_for_that_role(
    runs: BoundaryRuns, suite: Suite
) -> None:
    with pytest.raises(RunRefused, match="there is no attempt"):
        await runs.run("request-1", "qa", DIGEST, RUNTIME, retry=uuid.uuid4())
    made = await runs.run("request-2", "qa", DIGEST, RUNTIME)
    with pytest.raises(RunRefused, match="there is no attempt"):
        await runs.run("request-3", "builder", DIGEST, RUNTIME, retry=made.attempt_id)
    with pytest.raises(RunRefused, match="passed, not uncertain"):
        await runs.run("request-3", "qa", DIGEST, RUNTIME, retry=made.attempt_id)
    assert suite.launched == 1
