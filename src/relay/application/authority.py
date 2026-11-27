"""The Product Owner's authority, and the operator's statement of policy.

A model may sit between the person and Relay, and it runs commands. What it must not be
able to do is decide. So every command that decides asks for a passphrase, which the
person set and no model was told, and Relay keeps only a salted hash of it.

That is the whole of it, said plainly: a person who tells a model their passphrase has
given their authority away, and nothing here can tell.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from relay.persistence.database import Pool

# scrypt's cost: about a tenth of a second to check a guess, on purpose.
_N, _R, _P, _LENGTH = 2**15, 8, 1, 32
SHORTEST = 8


class Refused(Exception):
    """The passphrase was wrong, or missing, or cannot be what was asked."""


def _hash(passphrase: str, salt: bytes, n: int = _N, r: int = _R, p: int = _P) -> bytes:
    return hashlib.scrypt(
        passphrase.encode(), salt=salt, n=n, r=r, p=p, dklen=_LENGTH, maxmem=2**27
    )


def hashed(passphrase: str) -> str:
    """What is kept of a passphrase: how it was hashed, the salt, and the hash."""
    salt = secrets.token_bytes(16)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${_hash(passphrase, salt).hex()}"


def matches(passphrase: str, kept: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = kept.split("$")
        if scheme != "scrypt":
            return False
        found = _hash(passphrase, bytes.fromhex(salt), int(n), int(r), int(p))
        return hmac.compare_digest(found, bytes.fromhex(expected))
    except ValueError:
        return False


async def is_set(pool: Pool) -> bool:
    async with pool.connection() as connection:
        row = await (await connection.execute("SELECT passphrase FROM installation")).fetchone()
    return row is not None and row["passphrase"] is not None


async def require(pool: Pool, passphrase: str) -> None:
    """Refuse unless this is the Product Owner's passphrase."""
    async with pool.connection() as connection:
        row = await (await connection.execute("SELECT passphrase FROM installation")).fetchone()
    if row is None or row["passphrase"] is None:
        raise Refused("no passphrase has been set: run `relay init`")
    if not matches(passphrase, row["passphrase"]):
        raise Refused("that is not the passphrase")


async def set_first(pool: Pool, passphrase: str) -> bool:
    """Set the passphrase if none is set. False, and nothing changed, if one is: a second
    enrolment finds the installation and leaves it alone."""
    _require_usable(passphrase)
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "UPDATE installation SET passphrase = %s WHERE passphrase IS NULL",
            (hashed(passphrase),),
        )
    return cursor.rowcount == 1


async def change(pool: Pool, current: str, new: str) -> None:
    """Change the passphrase, by whoever knows the one it is now."""
    _require_usable(new)
    async with pool.connection() as connection, connection.transaction():
        row = await (
            await connection.execute("SELECT passphrase FROM installation FOR UPDATE")
        ).fetchone()
        if row is None or row["passphrase"] is None:
            raise Refused("no passphrase has been set: run `relay init`")
        if not matches(current, row["passphrase"]):
            raise Refused("that is not the passphrase")
        await connection.execute("UPDATE installation SET passphrase = %s", (hashed(new),))


def _require_usable(passphrase: str) -> None:
    if len(passphrase) < SHORTEST:
        raise Refused(f"a passphrase has at least {SHORTEST} characters")


@dataclass(frozen=True)
class Policy:
    """What the operator has stated of the policy this host is given from elsewhere
    (D-10): which statement it is, who made it and when."""

    statement: str
    revision: int
    stated_by: str
    stated_at: datetime


async def state_policy(pool: Pool, passphrase: str, statement: str, stated_by: str) -> Policy:
    """Put the operator's statement on record as a new revision. It is a launch
    condition, so it is the Product Owner's to make: the passphrase is asked for. A
    call being admitted under the statement as it was holds this until it is on record."""
    statement = statement.strip()
    if not statement:
        raise Refused("a statement of policy says something")
    await require(pool, passphrase)
    async with pool.connection() as connection, connection.transaction():
        row = await (
            await connection.execute(
                "UPDATE installation SET policy = jsonb_build_object("
                "'statement', %s::text, 'stated_by', %s::text, 'stated_at', now(), "
                "'revision', coalesce((policy->>'revision')::int, 0) + 1, "
                "'earlier', coalesce(policy->'earlier', '[]'::jsonb) || CASE WHEN policy ? "
                "'statement' THEN jsonb_build_array(policy - 'earlier') ELSE '[]'::jsonb END) "
                "RETURNING policy",
                (statement, stated_by),
            )
        ).fetchone()
    assert row is not None
    return _policy(row["policy"])


async def policy_of(pool: Pool) -> Policy | None:
    async with pool.connection() as connection:
        row = await (await connection.execute("SELECT policy FROM installation")).fetchone()
    said = row["policy"] if row else None
    return _policy(said) if said and said.get("statement") else None


def _policy(said: dict[str, Any]) -> Policy:
    return Policy(
        statement=str(said["statement"]),
        revision=int(said.get("revision", 0)),
        stated_by=str(said.get("stated_by", "")),
        stated_at=datetime.fromisoformat(str(said["stated_at"])),
    )


__all__ = [
    "SHORTEST",
    "Policy",
    "Refused",
    "change",
    "hashed",
    "is_set",
    "matches",
    "policy_of",
    "require",
    "set_first",
    "state_policy",
]
