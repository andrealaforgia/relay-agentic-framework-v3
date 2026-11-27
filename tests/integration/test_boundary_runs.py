"""A run of the boundary suite is paid for, and is on record before it is launched."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.boundaries import Boundaries
from relay.application.boundary_runs import Attempt, BoundaryRuns, RunRefused
from relay.application.workers import _lock_key
from relay.providers.boundary_suite import NotLaunched, Observation, SuiteResult

RUNTIME = {"cli": "9.9.9 (stub)", "os": "test", "remote_policy": "none, says the operator"}
DIGEST = "d" * 64
INSTALLATION = "01a20000-0000-7000-8000-000000000001"


async def asked(
    runs: BoundaryRuns,
    request: str,
    role: str = "qa",
    retry: uuid.UUID | None = None,
    digest: str = DIGEST,
) -> Attempt:
    """As the command does: the request on record first, with what it asks for, then the
    run it asks for."""

    async def what() -> Mapping[str, str]:
        return {role: digest}

    await runs.open_request(request, INSTALLATION, retry, what)
    return await runs.run(request, role, digest, RUNTIME, retry=retry)


class Suite:
    """Stands in for the paid run, and counts how many were launched."""

    def __init__(self) -> None:
        self.launched = 0
        self.holds = True
        self.hold: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.fails_with: Exception | None = None
        self.call = "returned"

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
            role,
            DIGEST,
            dict(runtime),
            (Observation("writes stay inside", self.holds, ""),),
            None,
            call=self.call,
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
    first = await asked(runs, "request-1", "qa")
    again = await asked(runs, "request-1", "qa")

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

    made = await asked(runs, "request-1", "qa")

    assert (made.state, made.detail) == ("failed", "writes stay inside")
    assert not await Boundaries(database).holds(DIGEST)
    assert (await asked(runs, "request-1", "qa")).state == "failed"
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
    first = asyncio.create_task(asked(runs, "request-1", "qa"))
    await suite.started.wait()

    replayed = await asked(runs, "request-1", "qa")
    assert (replayed.state, replayed.launched_now) == ("running", False)
    with pytest.raises(RunRefused, match="is being made now"):
        await asked(runs, "request-2", "qa")
    with pytest.raises(RunRefused, match="running, not uncertain"):
        await asked(runs, "request-3", "qa", replayed.attempt_id)
    assert suite.launched == 1

    await end_the_process_of(database, replayed.attempt_id)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)

    lost = await asked(runs, "request-1", "qa")
    assert (lost.state, lost.attempt_id) == ("uncertain", replayed.attempt_id)
    with pytest.raises(RunRefused, match=f"--retry {lost.attempt_id}"):
        await asked(runs, "request-7", "qa")
    assert suite.launched == 1  # never repeated by itself
    # 3C-S1B-2.4: a refusal is its request's answer, and is kept. The run that was
    # being made is gone now, and the requests refused because of it are refused still.
    for request, why in (("request-2", "being made now"), ("request-3", "running, not uncertain")):
        answered = await runs.of_request(request, "qa")
        assert answered is not None and answered.state == "refused" and why in answered.detail
        again = await asked(
            runs, request, "qa", replayed.attempt_id if request == "request-3" else None
        )
        assert (again.state, again.launched_now) == ("refused", False)
    assert suite.launched == 1

    suite.hold = None
    retried = await asked(runs, "request-4", "qa", lost.attempt_id)

    assert (retried.state, retried.launched_now) == ("passed", True)
    assert retried.attempt_id != lost.attempt_id and suite.launched == 2
    again = await asked(runs, "request-4", "qa", lost.attempt_id)
    assert (again.attempt_id, again.launched_now) == (retried.attempt_id, False)
    with pytest.raises(RunRefused, match="was retried already"):
        await asked(runs, "request-5", "qa", lost.attempt_id)
    assert suite.launched == 2
    assert (await asked(runs, "request-1", "qa")).state == "uncertain"  # as it was
    # With the uncertain one answered for, a new request for these bounds is an ordinary
    # one: and these bounds have just been seen to hold, so it runs nothing.
    assert (await asked(runs, "request-6", "qa")).state == "fresh"
    assert suite.launched == 2


async def test_a_run_that_ended_without_a_result_is_uncertain_and_says_how_to_retry(
    runs: BoundaryRuns, suite: Suite
) -> None:
    suite.fails_with = RuntimeError("the terminal went away")

    with pytest.raises(RunRefused, match="may have been paid for") as refused:
        await asked(runs, "request-1", "qa")

    known = await runs.of_request("request-1", "qa")
    assert known is not None and known.state == "uncertain"
    assert f"--retry {known.attempt_id}" in str(refused.value)
    suite.fails_with = None
    with pytest.raises(RunRefused, match="not repeated by itself"):
        await asked(runs, "request-2", "qa")
    assert suite.launched == 1


async def test_a_retry_names_an_attempt_there_is_for_that_role(
    runs: BoundaryRuns, suite: Suite
) -> None:
    with pytest.raises(RunRefused, match="there is no attempt"):
        await asked(runs, "request-1", "qa", uuid.uuid4())
    made = await asked(runs, "request-2", "qa")
    with pytest.raises(RunRefused, match="there is no attempt"):
        await asked(runs, "request-3", "builder", made.attempt_id)
    with pytest.raises(RunRefused, match="passed, not uncertain"):
        await asked(runs, "request-3", "qa", made.attempt_id)
    assert suite.launched == 1


async def test_a_second_request_finds_the_pass_the_first_paid_for_and_buys_nothing(
    database: AsyncConnectionPool, runs: BoundaryRuns, suite: Suite
) -> None:
    """3C-S1B-1.2: two operators each worked out that these bounds were owed a run. The
    first finishes before the second has its turn. Whether a pass is fresh is asked
    again there, under the boundary's lock."""

    async def owed() -> Mapping[str, str]:
        return {"qa": DIGEST}

    await runs.open_request("request-1", INSTALLATION, None, owed)
    await runs.open_request("request-2", INSTALLATION, None, owed)  # owed, as it then saw

    first = await runs.run("request-1", "qa", DIGEST, RUNTIME)
    second = await runs.run("request-2", "qa", DIGEST, RUNTIME)

    assert (first.state, first.launched_now) == ("passed", True)
    assert (second.state, second.launched_now) == ("fresh", False)
    assert suite.launched == 1
    # And sent again after that pass is stale, the second still buys nothing.
    async with database.connection() as connection:
        await connection.execute(
            "UPDATE boundary_passes SET observed_at = now() - interval '3 days'"
        )
    assert (await runs.run("request-2", "qa", DIGEST, RUNTIME)).state == "fresh"
    assert suite.launched == 1


async def test_a_request_is_on_record_with_all_it_asked_for_before_anything_is_run(
    runs: BoundaryRuns, suite: Suite
) -> None:
    """3C-S1B-1.1: what a request asks to have tried is fixed when it is first made, an
    empty set included. Sent again it asks for that, whatever has come to be owed since;
    and it is a request of one installation."""
    looked = 0

    async def nothing_owed() -> Mapping[str, str]:
        nonlocal looked
        looked += 1
        return {}

    async def owed_by_now() -> Mapping[str, str]:
        nonlocal looked
        looked += 1
        return {"qa": DIGEST}

    assert await runs.open_request("request-1", INSTALLATION, None, nothing_owed) == {}
    assert await runs.open_request("request-1", INSTALLATION, None, owed_by_now) == {}
    assert looked == 1 and suite.launched == 0
    assert await runs.to_launch("request-1", {}) == []

    with pytest.raises(RunRefused, match="another installation"):
        await runs.open_request(
            "request-1", "01a20000-0000-7000-8000-00000000000b", None, owed_by_now
        )
    with pytest.raises(RunRefused, match="for something else"):
        await runs.open_request("request-1", INSTALLATION, uuid.uuid4(), owed_by_now)
    assert await runs.open_request("request-2", INSTALLATION, None, owed_by_now) == {"qa": DIGEST}
    assert await runs.to_launch("request-2", {"qa": DIGEST}) == ["qa"]
    await runs.run("request-2", "qa", DIGEST, RUNTIME)
    assert await runs.to_launch("request-2", {"qa": DIGEST}) == []


async def test_a_run_that_never_reached_the_provider_cost_nothing_and_needs_nobody_asked(
    database: AsyncConnectionPool, runs: BoundaryRuns, suite: Suite
) -> None:
    """3C-S1B-1.3: the scene could not be made, or the CLI said it started nothing. Known,
    and not uncertain: the next ordinary request tries the bounds again."""
    suite.fails_with = NotLaunched("the suite could not listen for the run's connections")

    never = await asked(runs, "request-1")

    assert (never.state, never.launched_now) == ("not_launched", False)
    assert "could not listen" in never.detail
    assert not await Boundaries(database).holds(DIGEST)
    suite.fails_with, suite.call, suite.holds = None, "not_started", False
    refused_to_start = await asked(runs, "request-2")
    assert refused_to_start.state == "not_launched"
    assert not await Boundaries(database).holds(DIGEST)
    suite.call, suite.holds = "returned", True
    assert (await asked(runs, "request-3")).state == "passed"  # with nobody asked


async def test_a_call_that_was_launched_and_ended_with_no_result_is_uncertain_whatever_its_verdict(
    database: AsyncConnectionPool, runs: BoundaryRuns, suite: Suite
) -> None:
    """3C-S1B-1.3: the verdict of such a run is a failure, and is recorded as one, so
    nothing is authorised by it. But it is not a result to be bought again unasked: the
    call may have been paid for, and only a named retry buys another."""
    suite.call, suite.holds = "uncertain", False

    lost = await asked(runs, "request-1")

    assert lost.state == "uncertain" and "no result" in lost.detail
    assert not await Boundaries(database).holds(DIGEST)
    suite.call, suite.holds = "returned", True
    with pytest.raises(RunRefused, match=f"--retry {lost.attempt_id}"):
        await asked(runs, "request-2")
    assert suite.launched == 1
    assert (await asked(runs, "request-3", retry=lost.attempt_id)).state == "passed"
    assert suite.launched == 2 and await Boundaries(database).holds(DIGEST)


async def test_a_verdict_and_what_became_of_its_call_are_on_record_together_or_not_at_all(
    database: AsyncConnectionPool, runs: BoundaryRuns, suite: Suite, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3C-S1B-2.1: the provider's stream was cut short, and the process is lost as it
    records. There is no moment at which the failed verdict is on record without the
    fact that the call was lost: a new ordinary request launches nothing, and a named
    retry may launch one."""
    suite.call, suite.holds = "uncertain", False
    boundaries = runs._boundaries
    record_in = boundaries.record_in

    async def lost_as_it_records(connection: Any, result: SuiteResult) -> None:
        await record_in(connection, result)
        raise ConnectionError("the process was lost")

    monkeypatch.setattr(boundaries, "record_in", lost_as_it_records)
    with pytest.raises(ConnectionError):
        await asked(runs, "request-1")
    monkeypatch.undo()

    async with database.connection() as connection:
        verdicts = await (
            await connection.execute("SELECT count(*) AS n FROM boundary_passes")
        ).fetchone()
    assert verdicts is not None and verdicts["n"] == 0
    lost = await runs.of_request("request-1", "qa")
    assert lost is not None and lost.state == "uncertain"
    suite.call, suite.holds = "returned", True
    with pytest.raises(RunRefused, match=f"--retry {lost.attempt_id}"):
        await asked(runs, "request-2")
    assert suite.launched == 1
    assert (await asked(runs, "request-3", retry=lost.attempt_id)).state == "passed"
    assert suite.launched == 2


async def test_a_retry_is_not_made_beside_a_run_of_the_bounds_it_would_itself_run(
    database: AsyncConnectionPool, runs: BoundaryRuns, suite: Suite
) -> None:
    """3C-S1B-2.2: an uncertain attempt of the bounds as they were. The runtime has
    changed, and a run of today's bounds is being made. A retry of the old attempt would
    run today's bounds: it is refused while that run is live."""
    today = "e" * 64
    suite.fails_with = RuntimeError("the terminal went away")
    with pytest.raises(RunRefused, match="may have been paid for"):
        await asked(runs, "request-1")
    lost = await runs.of_request("request-1", "qa")
    assert lost is not None and lost.state == "uncertain"
    suite.fails_with, suite.launched, suite.hold = None, 0, asyncio.Event()
    suite.started.clear()
    live = asyncio.create_task(asked(runs, "request-2", digest=today))
    await suite.started.wait()

    with pytest.raises(RunRefused, match="is being made now"):
        await asked(runs, "request-3", retry=lost.attempt_id, digest=today)

    assert suite.launched == 1
    suite.hold.set()
    await live
    assert suite.launched == 1


async def test_a_request_is_not_spent_on_other_bounds_than_the_ones_it_asked_for(
    runs: BoundaryRuns, suite: Suite
) -> None:
    """3C-S1B-2.3: the request recorded one boundary and was stopped before its attempt.
    The CLI or the policy changed. Sent again it would be a run of another boundary: it
    is refused, for good, and says to make a new request. One that completed before the
    change still returns its receipt."""

    async def what() -> Mapping[str, str]:
        return {"qa": DIGEST}

    today = "e" * 64
    await runs.open_request("request-1", INSTALLATION, None, what)
    await runs.open_request("request-2", INSTALLATION, None, what)
    done = await runs.run("request-2", "qa", DIGEST, RUNTIME, exercising=DIGEST)

    with pytest.raises(RunRefused, match="make a new request"):
        await runs.run("request-1", "qa", DIGEST, RUNTIME, exercising=today)

    assert suite.launched == 1
    kept = await runs.run("request-1", "qa", DIGEST, RUNTIME, exercising=DIGEST)
    assert (kept.state, kept.launched_now) == ("refused", False)  # though it would match now
    replayed = await runs.run("request-2", "qa", DIGEST, RUNTIME, exercising=today)
    assert (replayed.attempt_id, replayed.state) == (done.attempt_id, "passed")
    assert suite.launched == 1
