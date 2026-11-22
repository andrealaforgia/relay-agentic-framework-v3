"""Whether the bounds a run would be launched with have been shown to hold on this host.

A pass is of one boundary: a role's tools and settings on one runtime. It is recorded
with everything the suite observed, and it goes stale. Part of what bounds a run is
policy this host is given from elsewhere and Relay cannot read, so the only evidence
that the boundary holds is that it was seen to, recently.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from psycopg.types.json import Jsonb

from relay.persistence.database import Pool
from relay.providers.boundary_suite import SuiteResult, as_recorded


class BoundaryNotShown(Exception):
    """The suite ran and the boundary did not hold. Nothing was recorded as a pass."""


class Boundaries:
    def __init__(self, pool: Pool, good_for: timedelta = timedelta(hours=24)) -> None:
        self._pool = pool
        self._good_for = good_for

    async def record(self, result: SuiteResult) -> None:
        """Record what a run of the suite found, whichever way it went, and raise if the
        boundary did not hold. A failure is kept: it takes away an earlier pass."""
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute(
                "INSERT INTO boundary_passes (pass_id, digest, role, runtime, observations, "
                "passed) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    uuid.uuid7(),
                    result.digest,
                    result.role,
                    Jsonb(result.runtime),
                    Jsonb(as_recorded(result)),
                    result.passed,
                ),
            )
        if not result.passed:
            failed = [each.probe for each in result.observations if not each.passed]
            raise BoundaryNotShown(f"the bounds of a {result.role} did not hold: {failed}")

    async def holds(self, digest: str) -> bool:
        """Whether the latest run of the suite on this exact boundary passed, and not too
        long ago."""
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT passed AND passed_at > clock_timestamp() - %s AS holds "
                    "FROM boundary_passes WHERE digest = %s ORDER BY passed_at DESC LIMIT 1",
                    (self._good_for, digest),
                )
            ).fetchone()
        return bool(row and row["holds"])
