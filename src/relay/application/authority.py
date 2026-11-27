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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from relay.persistence.database import Connection, Pool

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
    require_usable(passphrase)
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "UPDATE installation SET passphrase = %s WHERE passphrase IS NULL",
            (hashed(passphrase),),
        )
    return cursor.rowcount == 1


async def change(pool: Pool, current: str, new: str, request_id: str | None = None) -> str:
    """Change the passphrase, by whoever knows the one it is now. Says "changed", or, for
    a request that did this already, "changed, as on record": the reply to a request may
    be lost, and asked again with the old passphrase it must not look like a refusal."""
    require_usable(new)
    async with pool.connection() as connection, connection.transaction():
        row = await _locked_installation(connection)
        done = await _receipt(connection, request_id, "passphrase", "")
        if done is not None:
            return "changed, as on record"
        _require_matches(current, row["passphrase"])
        await connection.execute("UPDATE installation SET passphrase = %s", (hashed(new),))
        await _keep_receipt(connection, row, request_id, "passphrase", "", {"changed": True})
    return "changed"


async def receipt_of(pool: Pool, request_id: str) -> dict[str, Any] | None:
    """What a request did, if it did anything: asked before a passphrase is asked for."""
    async with pool.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT action, outcome FROM installation_receipts WHERE request_id = %s",
                (request_id,),
            )
        ).fetchone()
    return {"action": row["action"], **row["outcome"]} if row else None


async def _locked_installation(connection: Connection) -> dict[str, Any]:
    row = await (
        await connection.execute(
            "SELECT installation_id, passphrase, policy FROM installation FOR UPDATE"
        )
    ).fetchone()
    assert row is not None
    return row


def _require_matches(passphrase: str, kept: str | None) -> None:
    if kept is None:
        raise Refused("no passphrase has been set: run `relay init`")
    if not matches(passphrase, kept):
        raise Refused("that is not the passphrase")


def _digest(asked: str) -> str:
    return hashlib.sha256(asked.encode()).hexdigest()


async def _receipt(
    connection: Connection, request_id: str | None, action: str, asked: str
) -> dict[str, Any] | None:
    if request_id is None:
        return None
    row = await (
        await connection.execute(
            "SELECT action, asked, outcome FROM installation_receipts WHERE request_id = %s",
            (request_id,),
        )
    ).fetchone()
    if row is None:
        return None
    if row["action"] != action or row["asked"] != _digest(asked):
        raise Refused(
            f"request {request_id} was for something else: a request's identity is used "
            "again only to ask for the same thing"
        )
    return dict(row["outcome"])


async def _keep_receipt(
    connection: Connection,
    installation: Mapping[str, Any],
    request_id: str | None,
    action: str,
    asked: str,
    outcome: Mapping[str, Any],
) -> None:
    if request_id is None:
        return
    await connection.execute(
        "INSERT INTO installation_receipts (request_id, installation, action, asked, outcome) "
        "VALUES (%s, %s, %s, %s, %s)",
        (request_id, installation["installation_id"], action, _digest(asked), Jsonb(dict(outcome))),
    )


def require_usable(passphrase: str) -> None:
    if len(passphrase) < SHORTEST:
        raise Refused(f"a passphrase has at least {SHORTEST} characters")


@dataclass(frozen=True)
class Policy:
    """What the operator has stated of the policy this host is given from elsewhere
    (D-10): which statement it is, who made it and when."""

    statement: str
    revision: int
    # Empty, and None: a statement put on record before who made it and when were kept.
    stated_by: str
    stated_at: datetime | None


async def state_policy(
    pool: Pool, passphrase: str, statement: str, stated_by: str, request_id: str | None = None
) -> Policy:
    """Put the operator's statement on record as a new revision. It is a launch
    condition, so it is the Product Owner's to make: the passphrase is asked for, and
    checked in the transaction that writes, under the installation's lock: a passphrase
    changed a moment ago authorises nothing. A call being admitted under the statement
    as it was holds this until it is on record. A request that did this already is
    answered with the revision it made, and makes no other."""
    statement = statement.strip()
    if not statement:
        raise Refused("a statement of policy says something")
    async with pool.connection() as connection, connection.transaction():
        installation = await _locked_installation(connection)
        done = await _receipt(connection, request_id, "policy", statement)
        if done is not None:
            return _policy(done)
        _require_matches(passphrase, installation["passphrase"])
        row = await (
            await connection.execute(
                "UPDATE installation SET policy = jsonb_build_object("
                "'statement', %s::text, 'stated_by', %s::text, 'stated_at', now(), "
                "'revision', coalesce((policy->>'revision')::int, 0) + 1, "
                "'earlier', coalesce(policy->'earlier', '[]'::jsonb) || CASE WHEN policy ? "
                "'statement' THEN jsonb_build_array(policy - 'earlier') ELSE '[]'::jsonb END) "
                "RETURNING policy - 'earlier' AS policy",
                (statement, stated_by),
            )
        ).fetchone()
        assert row is not None
        await _keep_receipt(
            connection, installation, request_id, "policy", statement, row["policy"]
        )
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
        stated_at=datetime.fromisoformat(str(said["stated_at"])) if said.get("stated_at") else None,
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
    "receipt_of",
    "require",
    "require_usable",
    "set_first",
    "state_policy",
]
