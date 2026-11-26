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

from relay.persistence.database import Connection, Pool
from relay.providers.boundary_suite import SuiteResult, as_recorded


class BoundaryNotShown(Exception):
    """The suite ran and the boundary did not hold. Nothing was recorded as a pass."""


class Boundaries:
    def __init__(self, pool: Pool, good_for: timedelta = timedelta(hours=24)) -> None:
        self._pool = pool
        self._good_for = good_for

    async def record(self, result: SuiteResult) -> None:
        """Record what a run of the suite found, whichever way it went, and raise if the
        boundary did not hold. A failure is kept: it takes away an earlier pass. A run is
        recorded once however often it is handed over, as of when it was made."""
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute(
                "INSERT INTO boundary_passes (pass_id, digest, role, runtime, observations, "
                "passed, observed_at) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (pass_id) DO NOTHING",
                (
                    result.run_id,
                    result.digest,
                    result.role,
                    Jsonb(result.runtime),
                    Jsonb(as_recorded(result)),
                    result.passed,
                    result.observed_at,
                ),
            )
        if not result.passed:
            failed = [each.probe for each in result.observations if not each.passed]
            raise BoundaryNotShown(f"the bounds of a {result.role} did not hold: {failed}")

    async def holds(self, digest: str) -> bool:
        """Whether the latest run of the suite on this exact boundary passed, and not too
        long ago."""
        async with self._pool.connection() as connection:
            return await self.authorising(connection, digest) is not None

    async def authorising(self, connection: Connection, digest: str) -> uuid.UUID | None:
        """The assessment that allows a launch within this boundary now, or None: the
        run of the suite most lately made on it, if it passed and is not stale. Asked on
        the connection, and so in the transaction, of whoever is about to launch."""
        row = await (
            await connection.execute(
                "SELECT pass_id, passed AND observed_at > clock_timestamp() - %s AS holds "
                "FROM boundary_passes WHERE digest = %s "
                "ORDER BY observed_at DESC, pass_id DESC LIMIT 1",
                (self._good_for, digest),
            )
        ).fetchone()
        held: uuid.UUID | None = row["pass_id"] if row and row["holds"] else None
        return held
