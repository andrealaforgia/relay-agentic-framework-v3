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
        """Record a pass. A run of the suite that did not pass is not recorded as
        anything: it is an error, naming what failed."""
        if not result.passed:
            failed = [each.probe for each in result.observations if not each.passed]
            raise BoundaryNotShown(f"the bounds of a {result.role} did not hold: {failed}")
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute(
                "INSERT INTO boundary_passes (pass_id, digest, role, runtime, observations) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    uuid.uuid7(),
                    result.digest,
                    result.role,
                    Jsonb(result.runtime),
                    Jsonb(as_recorded(result)),
                ),
            )

    async def holds(self, digest: str) -> bool:
        """Whether this exact boundary has a pass that is not stale."""
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT 1 FROM boundary_passes WHERE digest = %s "
                    "AND passed_at > clock_timestamp() - %s LIMIT 1",
                    (digest, self._good_for),
                )
            ).fetchone()
        return row is not None
