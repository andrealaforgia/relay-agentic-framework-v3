"""A pass of a boundary is recorded with what was observed, is of exactly that boundary,
and goes stale."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.boundaries import Boundaries, BoundaryNotShown
from relay.providers.boundary_suite import Observation, SuiteResult

RUNTIME = {"cli": "2.1.330", "os": "macOS", "suite": "1", "managed_settings": "none"}


def suite(digest: str = "d" * 64, *observed: Observation) -> SuiteResult:
    seen = observed or (Observation("a file in the workspace is read", True, "INSIDE"),)
    return SuiteResult("builder", digest, RUNTIME, seen, Decimal("0.005"))


async def test_a_boundary_holds_once_it_has_been_seen_to(database: AsyncConnectionPool) -> None:
    boundaries = Boundaries(database)
    assert not await boundaries.holds("d" * 64)

    await boundaries.record(suite())

    assert await boundaries.holds("d" * 64)
    assert not await boundaries.holds("e" * 64)  # another boundary has no pass
    async with database.connection() as connection:
        row = await (await connection.execute("SELECT * FROM boundary_passes")).fetchone()
    assert row is not None
    assert (row["role"], row["runtime"]) == ("builder", RUNTIME)
    assert row["cost_usd"] == Decimal("0.005")  # what the run cost is kept with it
    assert row["observations"] == [
        {"probe": "a file in the workspace is read", "passed": True, "saw": "INSIDE"}
    ]


async def test_a_run_of_the_suite_that_failed_is_not_a_pass(database: AsyncConnectionPool) -> None:
    boundaries = Boundaries(database)
    leaked = suite(
        "d" * 64,
        Observation("a file in the workspace is read", True, ""),
        Observation("the shell does not read the home directory", False, "HOME-WORD"),
    )

    with pytest.raises(BoundaryNotShown, match="the shell does not read the home directory"):
        await boundaries.record(leaked)

    assert not await boundaries.holds("d" * 64)
    async with database.connection() as connection:
        row = await (await connection.execute("SELECT passed FROM boundary_passes")).fetchone()
    assert row is not None and row["passed"] is False  # kept, as what was seen


async def test_a_later_failure_takes_an_earlier_pass_away_until_it_passes_again(
    database: AsyncConnectionPool,
) -> None:
    """3B-S2-1.2: the latest run of the suite is what is true of a boundary."""
    boundaries = Boundaries(database)
    await boundaries.record(suite())
    assert await boundaries.holds("d" * 64)

    failing = suite("d" * 64, Observation("the shell is refused the network", False, "200"))
    with pytest.raises(BoundaryNotShown):
        await boundaries.record(failing)
    assert not await boundaries.holds("d" * 64)

    await boundaries.record(suite())
    assert await boundaries.holds("d" * 64)


async def test_a_suite_that_observed_nothing_is_not_a_pass(database: AsyncConnectionPool) -> None:
    with pytest.raises(BoundaryNotShown):
        await Boundaries(database).record(SuiteResult("builder", "d" * 64, RUNTIME, (), None))


async def test_a_pass_goes_stale(database: AsyncConnectionPool) -> None:
    """Part of what bounds a run is policy Relay cannot read, so a pass is evidence only
    for a while."""
    # Two hours old, by the clock of the host that made it: the database's own clock
    # need not agree with that one to the millisecond.
    made = datetime.now(UTC) - timedelta(hours=2)
    await Boundaries(database).record(replace(suite(), observed_at=made))

    assert await Boundaries(database, good_for=timedelta(hours=3)).holds("d" * 64)
    assert not await Boundaries(database, good_for=timedelta(hours=1)).holds("d" * 64)


async def test_an_earlier_pass_handed_over_again_does_not_undo_a_later_failure(
    database: AsyncConnectionPool,
) -> None:
    """3B-S2-3.5: a run of the suite is one assessment however often it is recorded, as
    of when it was made."""
    boundaries = Boundaries(database)
    now = datetime.now(UTC)
    passed = replace(suite(), observed_at=now - timedelta(minutes=10))
    failed = replace(
        suite("d" * 64, Observation("the shell is refused the network", False, "200")),
        observed_at=now - timedelta(minutes=5),
    )
    await boundaries.record(passed)
    with pytest.raises(BoundaryNotShown):
        await boundaries.record(failed)

    await boundaries.record(passed)

    assert not await boundaries.holds("d" * 64)
    async with database.connection() as connection:
        rows = await (await connection.execute("SELECT pass_id FROM boundary_passes")).fetchall()
    assert sorted(row["pass_id"] for row in rows) == sorted([passed.run_id, failed.run_id])


async def test_the_latest_assessment_is_the_one_observed_last_not_the_one_made_up_last(
    database: AsyncConnectionPool,
) -> None:
    """3B-S2-3.5: a run's identity may be newer than what it observed."""
    boundaries = Boundaries(database)
    now = datetime.now(UTC)
    failed = replace(
        suite("d" * 64, Observation("the shell is refused the network", False, "200")),
        observed_at=now - timedelta(minutes=5),
    )
    passed = replace(suite(), observed_at=now - timedelta(minutes=10))
    assert passed.run_id > failed.run_id
    with pytest.raises(BoundaryNotShown):
        await boundaries.record(failed)

    await boundaries.record(passed)

    assert not await boundaries.holds("d" * 64)


async def test_a_pass_recorded_late_is_as_old_as_the_run_it_is_of(
    database: AsyncConnectionPool,
) -> None:
    """3B-S2-3.5: a delayed write gives old observations no new lease."""
    boundaries = Boundaries(database)

    await boundaries.record(replace(suite(), observed_at=datetime.now(UTC) - timedelta(hours=25)))

    assert not await boundaries.holds("d" * 64)
