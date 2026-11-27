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

from relay.application.boundaries import Boundaries
from relay.application.workers import _lock_key, is_alive
from relay.persistence.database import Connection, Pool
from relay.providers.boundary_suite import NotLaunched, SuiteResult

Suite = Callable[[str, Mapping[str, str]], Awaitable[SuiteResult]]


FRESH = "these bounds had lately been seen to hold: nothing was run"
REFUSED = "refused: "


class RunRefused(Exception):
    """No run was made, and nothing was spent: said why."""


@dataclass(frozen=True)
class Attempt:
    attempt_id: uuid.UUID
    request_id: str
    role: str
    # fresh: a pass of these bounds was on record when its turn came: nothing was run.
    # refused: the request was answered with a refusal for this role, and that is its
    # answer for good.
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

    async def open_request(
        self,
        request_id: str,
        installation: str,
        retry: uuid.UUID | None,
        work_out: Callable[[], Awaitable[Mapping[str, str]]],
    ) -> dict[str, str]:
        """Put the request on record, with everything it asks to have tried, before
        anything is launched; or, if it is on record, say what it asked for then. What
        has come to be owed since is no part of a request sent again."""
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))", (request_id,)
            )
            known = await (
                await connection.execute(
                    "SELECT installation::text AS installation, retries, asked "
                    "FROM boundary_requests WHERE request_id = %s",
                    (request_id,),
                )
            ).fetchone()
            if known is not None:
                if known["installation"] != installation:
                    raise RunRefused(
                        f"request {request_id} was made of another installation "
                        f"({known['installation']}): it is not answered from this one"
                    )
                if known["retries"] != retry:
                    raise RunRefused(
                        f"request {request_id} was for something else: a request's identity "
                        "is used again only to ask for the same thing"
                    )
                return {str(each["role"]): str(each["digest"]) for each in known["asked"]}
            asked = dict(await work_out())
            await connection.execute(
                "INSERT INTO boundary_requests (request_id, installation, retries, asked) "
                "VALUES (%s, %s, %s, %s)",
                (
                    request_id,
                    installation,
                    retry,
                    Jsonb([{"role": role, "digest": digest} for role, digest in asked.items()]),
                ),
            )
            return asked

    async def to_launch(self, request_id: str, asked: Mapping[str, str]) -> list[str]:
        """The roles of a request for which no attempt is on record yet: what sending it
        now may still buy."""
        return [role for role in asked if await self.of_request(request_id, role) is None]

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
        exercising: str | None = None,
    ) -> Attempt:
        """Make the run this request asks for, if it has not been made; otherwise say what
        is known of the one that was. A request sent again launches nothing.

        `digest` is the boundary the request asked to have tried, and `exercising` the
        boundary a run made now would be a run of. If they differ, the runtime has
        changed since the request was made, and the request is not spent on another
        boundary than its own. A refusal is the request's answer, and is kept: sent
        again, the request is refused again and buys nothing, whatever has changed."""
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
                refusal: RunRefused | None = None
                try:
                    if exercising is not None and exercising != digest:
                        raise RunRefused(
                            f"what bounds a {role} on this host has changed since request "
                            f"{request_id} was made: it asked for a run of other bounds than "
                            "a run made now would be of. Nothing is run: make a new request"
                        )
                    await self._require_may_launch(connection, role, digest, retry)
                except RunRefused as refused:
                    refusal = refused
                if refusal is not None:
                    # On record against the request and the role. It names no attempt it
                    # retries: a refused retry does not use up the one retry there is.
                    await connection.execute(
                        "INSERT INTO boundary_runs (attempt_id, request_id, role, digest, "
                        "runtime, not_launched) VALUES (%s, %s, %s, %s, %s, %s)",
                        (
                            attempt_id,
                            request_id,
                            role,
                            digest,
                            Jsonb(dict(runtime)),
                            REFUSED + str(refusal),
                        ),
                    )
                # Asked again here, under the boundary's lock: another request may have
                # paid for a pass of these bounds while this one waited its turn.
                fresh = (
                    refusal is None
                    and retry is None
                    and await self._boundaries.authorising(connection, digest) is not None
                )
                if refusal is None:
                    await connection.execute(
                        "INSERT INTO boundary_runs (attempt_id, request_id, role, digest, "
                        "runtime, retries, not_launched) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (
                            attempt_id,
                            request_id,
                            role,
                            digest,
                            Jsonb(dict(runtime)),
                            retry,
                            FRESH if fresh else None,
                        ),
                    )
                if fresh:
                    made = await _attempt(connection, "r.attempt_id = %s", (attempt_id,))
                    assert made is not None
                    return made
            if refusal is not None:
                raise refusal
            try:
                result = await self._suite(role, runtime)
            except NotLaunched as never:
                # Established before the provider was called: nothing was spent, and
                # nobody needs asking before these bounds are tried again.
                return await self._settled(attempt_id, not_launched=str(never))
            except Exception as failed:
                # After the provider may have been called, and with nothing to show for
                # it: the attempt is left without a result, and is uncertain once this
                # process lets go of it. It is not tried again by itself.
                raise RunRefused(
                    f"the run for the {role} ended without a result, and may have been paid "
                    f"for: attempt {attempt_id} is uncertain ({failed!r}). "
                    f"`relay doctor --boundary --retry {attempt_id}` buys another"
                ) from failed
            # The verdict is this attempt's: recorded under its identity, once. A run
            # whose call did not come back with a result showed nothing to hold, and its
            # verdict is recorded as the failure it is: nothing is authorised by it.
            # In one transaction with what became of the call: there is no moment at
            # which the failed verdict of a lost call is on record without the fact that
            # the call was lost, to be read as an ordinary failure and bought again.
            if result.call == "not_started":
                return await self._settled(
                    attempt_id,
                    not_launched="the provider's CLI did not start the run",
                    verdict=dataclasses.replace(result, run_id=attempt_id),
                )
            return await self._settled(
                attempt_id, call=result.call, verdict=dataclasses.replace(result, run_id=attempt_id)
            )
        finally:
            with contextlib.suppress(Exception):
                await held.execute("SELECT pg_advisory_unlock_all()")
                await held.set_autocommit(False)
            await self._pool.putconn(held)

    async def _settled(
        self,
        attempt_id: uuid.UUID,
        *,
        not_launched: str | None = None,
        call: str | None = None,
        verdict: SuiteResult | None = None,
    ) -> Attempt:
        """Put on record what became of the call itself, with its verdict if it has one."""
        async with self._pool.connection() as connection, connection.transaction():
            if verdict is not None:
                await self._boundaries.record_in(connection, verdict)
            await connection.execute(
                "UPDATE boundary_runs SET not_launched = %s, call = %s WHERE attempt_id = %s",
                (not_launched, call, attempt_id),
            )
            made = await _attempt(connection, "r.attempt_id = %s", (attempt_id,))
        assert made is not None
        return dataclasses.replace(made, launched_now=not_launched is None)

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
        # And whatever it is, a run of these bounds is not made beside another: the
        # bounds a retry runs are today's, which may be another boundary than the
        # attempt's, with a run of its own being made or lost.
        others = await (
            await connection.execute(
                "SELECT attempt_id FROM boundary_runs r WHERE digest = %s "
                "AND attempt_id IS DISTINCT FROM %s AND NOT EXISTS "
                "(SELECT 1 FROM boundary_runs later WHERE later.retries = r.attempt_id) "
                "ORDER BY started_at DESC",
                (digest, retry),
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
            "SELECT r.attempt_id, r.request_id, r.role, r.not_launched, r.call, p.passed, "
            "p.observations "
            "FROM boundary_runs r LEFT JOIN boundary_passes p ON p.pass_id = r.attempt_id "
            f"WHERE {where}",
            values,
        )
    ).fetchone()
    if row is None:
        return None
    failed = [
        str(each.get("probe"))
        for each in (row["observations"] or [])
        if isinstance(each, dict) and not each.get("passed", True)
    ]
    if row["not_launched"] == FRESH:
        state, detail = "fresh", FRESH
    elif row["not_launched"] is not None and str(row["not_launched"]).startswith(REFUSED):
        state, detail = "refused", str(row["not_launched"])[len(REFUSED) :]
    elif row["not_launched"] is not None:
        state, detail = "not_launched", str(row["not_launched"])
    elif row["call"] == "uncertain":
        # Launched, and it ended with no result: whatever verdict is beside it, the
        # call may have been paid for, and only a person buys another.
        state, detail = "uncertain", "the call was launched and ended with no result"
    elif row["passed"] is not None:
        state, detail = ("passed", "") if row["passed"] else ("failed", "; ".join(failed))
    elif await is_alive(connection, row["attempt_id"]):
        state, detail = "running", ""
    else:
        state, detail = "uncertain", "launched, perhaps, and nothing was recorded"
    return Attempt(row["attempt_id"], row["request_id"], row["role"], state, detail)
