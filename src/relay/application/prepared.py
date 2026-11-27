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
                "expires_at <= now() AS expired FROM prepared_actions WHERE code = %s",
                (code,),
            )
        ).fetchone()
    return Prepared(**row) if row else None


async def end(pool: Pool, code: str, why: str) -> None:
    """A prepared action that will never be confirmed: stale, or expired. Said once."""
    async with pool.connection() as connection:
        await connection.execute(
            "UPDATE prepared_actions SET ended = %s WHERE code = %s AND ended IS NULL",
            (why, code),
        )
