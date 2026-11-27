"""Actions prepared for the Product Owner to confirm.

Preparing decides nothing and needs no authority: it records what someone proposes, as
they saw the record, and gives back a short code. Confirming is the Product Owner's, at a
terminal, with the passphrase: `relay confirm <code>`.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta

from relay.persistence.database import Pool

GOOD_FOR = timedelta(hours=1)


@dataclass(frozen=True)
class Prepared:
    code: str
    project_id: str
    verb: str
    target: str
    said: str | None
    reason: str | None
    why: str
    shown: str
    basis: str
    expired: bool
    ended: str | None
    # The request of the confirmation that has taken it, if one has.
    consumed_by: str | None = None


async def prepare(
    pool: Pool,
    project_id: str,
    verb: str,
    target: str,
    said: str | None,
    reason: str | None,
    why: str,
    shown: str,
    basis: str,
    good_for: timedelta = GOOD_FOR,
) -> Prepared:
    if not why.strip():
        raise ValueError("an action is prepared with why it is proposed")
    code = secrets.token_hex(4)
    async with pool.connection() as connection:
        await connection.execute(
            "INSERT INTO prepared_actions (code, project_id, verb, target, said, reason, why, "
            "shown, basis, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now() + %s)",
            (code, project_id, verb, target, said, reason, why.strip(), shown, basis, good_for),
        )
    found = await find(pool, code)
    assert found is not None
    return found


async def find(pool: Pool, code: str) -> Prepared | None:
    async with pool.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT code, project_id, verb, target, said, reason, why, shown, basis, ended, "
                "consumed_by, expires_at <= now() AND consumed_by IS NULL AS expired "
                "FROM prepared_actions WHERE code = %s",
                (code,),
            )
        ).fetchone()
    return Prepared(**row) if row else None


async def end(pool: Pool, code: str, why: str) -> None:
    """A prepared action that will never be confirmed: stale, or expired. Said once."""
    async with pool.connection() as connection:
        await connection.execute(
            # Never of a code a confirmation has taken: that one is settled by what its
            # confirmation decides, and by nothing said of it meanwhile.
            "UPDATE prepared_actions SET ended = %s WHERE code = %s AND ended IS NULL "
            "AND consumed_by IS NULL",
            (why, code),
        )


async def consume(pool: Pool, code: str, request_id: str) -> bool:
    """Take a code for the confirmation about to carry it out: once, and only while it is
    neither ended nor expired, as the database's clock has it at this moment. True if
    this request has it. A person who took their time at the prompt finds here that the
    hour has passed, and two terminals with one code are one after the other."""
    async with pool.connection() as connection, connection.transaction():
        taken = await (
            await connection.execute(
                # Taken in time, a code is this request's from then on, to be carried out
                # and answered by its receipt however long that takes: taken again by
                # the same request, after a crash or from a second terminal, it is the
                # same taking, whatever the hour.
                "UPDATE prepared_actions SET consumed_by = %s WHERE code = %s AND ended IS NULL "
                "AND ((consumed_by IS NULL AND expires_at > now()) OR consumed_by = %s) "
                "RETURNING code",
                (request_id, code, request_id),
            )
        ).fetchone()
        if taken is None:
            # Expired while the person was looking at it: said once, for whoever asks.
            await connection.execute(
                "UPDATE prepared_actions SET ended = %s WHERE code = %s AND ended IS NULL "
                "AND consumed_by IS NULL AND expires_at <= now()",
                ("this expired unused, an hour after it was prepared", code),
            )
    return taken is not None
