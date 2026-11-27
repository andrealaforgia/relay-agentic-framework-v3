"""Runs of the boundary suite, as requests that are on record before they cost anything.

The suite makes a real, paid call. A person who asks for a run and does not hear back
(the terminal closed, the machine slept) asks again, and must not pay again: the request
has an identity, and under that identity there is one attempt for a role, whatever is
known of it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from relay.application.boundaries import Boundaries, BoundaryNotShown
from relay.application.workers import _lock_key, is_alive
from relay.persistence.database import Connection, Pool
from relay.providers.boundary_suite import SuiteResult

Suite = Callable[[str, Mapping[str, str]], Awaitable[SuiteResult]]


class RunRefused(Exception):
    """No run was made, and nothing was spent: said why."""


@dataclass(frozen=True)
class Attempt:
    attempt_id: uuid.UUID
    request_id: str
    role: str
    # passed, failed: the suite ran and that is what it showed.
    # running: its process is alive and has recorded nothing yet.
    # uncertain: its process is gone and nothing was recorded. It may have been paid for.
    # not_launched: known never to have started.
    state: str
    detail: str = ""
    # Whether this call is the one that launched it: false for anything found on record.
    launched_now: bool = False


class BoundaryRuns:
    def __init__(self, pool: Pool, boundaries: Boundaries, suite: Suite) -> None:
        self._pool = pool
        self._boundaries = boundaries
        self._suite = suite

    async def of_request(self, request_id: str, role: str) -> Attempt | None:
        async with self._pool.connection() as connection:
            return await _attempt(
                connection, "r.request_id = %s AND r.role = %s", (request_id, role)
            )

    async def run(
        self,
        request_id: str,
        role: str,
        digest: str,
        runtime: Mapping[str, str],
        *,
        retry: uuid.UUID | None = None,
    ) -> Attempt:
        """Make the run this request asks for, if it has not been made; otherwise say what
        is known of the one that was. A request sent again launches nothing."""
        attempt_id = uuid.uuid7()
        held = await self._pool.getconn()
        try:
            await held.set_autocommit(True)
            # The lock first, and the row after: from the moment the attempt is on record
            # until this process ends or records a result, it is seen to be running.
            await held.execute("SELECT pg_advisory_lock(%s)", (_lock_key(attempt_id),))
            async with self._pool.connection() as connection, connection.transaction():
                # One at a time for a boundary: two requests for the same bounds do not
                # both find nothing running and both launch.
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (digest,)
                )
                known = await _attempt(
                    connection, "r.request_id = %s AND r.role = %s", (request_id, role)
                )
                if known is not None:
                    return known
                await self._require_may_launch(connection, role, digest, retry)
                await connection.execute(
                    "INSERT INTO boundary_runs (attempt_id, request_id, role, digest, runtime, "
                    "retries) VALUES (%s, %s, %s, %s, %s, %s)",
                    (attempt_id, request_id, role, digest, Jsonb(dict(runtime)), retry),
                )
            try:
                result = await self._suite(role, runtime)
            except Exception as failed:
                # Whether anything was launched before this is not known here: the
                # attempt is left without a result, and is uncertain once this process
                # lets go of it. It is not tried again by itself.
                raise RunRefused(
                    f"the run for the {role} ended without a result, and may have been paid "
                    f"for: attempt {attempt_id} is uncertain ({failed!r}). "
                    f"`relay doctor --boundary --retry {attempt_id}` buys another"
                ) from failed
            # The result is this attempt's: recorded under its identity, once.
            with contextlib.suppress(BoundaryNotShown):
                await self._boundaries.record(dataclasses.replace(result, run_id=attempt_id))
            async with self._pool.connection() as connection:
                made = await _attempt(connection, "r.attempt_id = %s", (attempt_id,))
            assert made is not None
            return dataclasses.replace(made, launched_now=True)
        finally:
            with contextlib.suppress(Exception):
                await held.execute("SELECT pg_advisory_unlock_all()")
                await held.set_autocommit(False)
            await self._pool.putconn(held)

    async def _require_may_launch(
        self, connection: Connection, role: str, digest: str, retry: uuid.UUID | None
    ) -> None:
        if retry is not None:
            retried = await _attempt(connection, "r.attempt_id = %s", (retry,))
            if retried is None or retried.role != role:
                raise RunRefused(f"there is no attempt {retry} for the {role} to retry")
            if retried.state != "uncertain":
                raise RunRefused(
                    f"attempt {retry} is {retried.state}, not uncertain: there is nothing to retry"
                )
            again = await _attempt(connection, "r.retries = %s", (retry,))
            if again is not None:
                raise RunRefused(
                    f"attempt {retry} was retried already, by attempt {again.attempt_id} "
                    f"(request {again.request_id}), which is {again.state}"
                )
            return
        others = await (
            await connection.execute(
                "SELECT attempt_id FROM boundary_runs r WHERE digest = %s AND NOT EXISTS "
                "(SELECT 1 FROM boundary_runs later WHERE later.retries = r.attempt_id) "
                "ORDER BY started_at DESC",
                (digest,),
            )
        ).fetchall()
        for other in others:
            standing = await _attempt(connection, "r.attempt_id = %s", (other["attempt_id"],))
            assert standing is not None
            if standing.state == "running":
                raise RunRefused(
                    f"a run for these bounds is being made now (attempt {standing.attempt_id}, "
                    f"request {standing.request_id}): nothing more is launched"
                )
            if standing.state == "uncertain":
                raise RunRefused(
                    f"attempt {standing.attempt_id} for these bounds was launched and nothing "
                    "is known of how it ended. It may have been paid for, and is not repeated "
                    f"by itself: `relay doctor --boundary --retry {standing.attempt_id}` buys "
                    "another"
                )


async def _attempt(
    connection: Connection, where: str, values: tuple[object, ...]
) -> Attempt | None:
    row = await (
        await connection.execute(
            "SELECT r.attempt_id, r.request_id, r.role, r.not_launched, p.passed, p.observations "
            "FROM boundary_runs r LEFT JOIN boundary_passes p ON p.pass_id = r.attempt_id "
            f"WHERE {where}",
            values,
        )
    ).fetchone()
    if row is None:
        return None
    if row["passed"] is not None:
        failed = [
            str(each.get("probe"))
            for each in row["observations"]
            if isinstance(each, dict) and not each.get("passed", True)
        ]
        state, detail = ("passed", "") if row["passed"] else ("failed", "; ".join(failed))
    elif row["not_launched"] is not None:
        state, detail = "not_launched", str(row["not_launched"])
    elif await is_alive(connection, row["attempt_id"]):
        state, detail = "running", ""
    else:
        state, detail = "uncertain", "launched, perhaps, and nothing was recorded"
    return Attempt(row["attempt_id"], row["request_id"], row["role"], state, detail)
