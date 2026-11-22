"""A pass of a boundary is recorded with what was observed, is of exactly that boundary,
and goes stale."""

from __future__ import annotations

from datetime import timedelta
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


async def test_a_suite_that_observed_nothing_is_not_a_pass(database: AsyncConnectionPool) -> None:
    with pytest.raises(BoundaryNotShown):
        await Boundaries(database).record(SuiteResult("builder", "d" * 64, RUNTIME, (), None))


async def test_a_pass_goes_stale(database: AsyncConnectionPool) -> None:
    """Part of what bounds a run is policy Relay cannot read, so a pass is evidence only
    for a while."""
    await Boundaries(database).record(suite())

    assert await Boundaries(database, good_for=timedelta(hours=1)).holds("d" * 64)
    assert not await Boundaries(database, good_for=timedelta(0)).holds("d" * 64)
