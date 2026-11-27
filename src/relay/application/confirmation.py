"""How a decision is known to be the Product Owner's (S3-1.8).

A command that decides reads the passphrase at the controlling terminal, checks it, and
says so here. The service that then records the decision writes that with the receipt,
and holds it to the passphrase as it is where the decision is made: a passphrase changed
between its being checked and the decision committing confirms nothing.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from relay.domain.story import ActionRefused
from relay.persistence.database import Connection

_CONFIRMED: ContextVar[int | None] = ContextVar("confirmed", default=None)

AT_THE_TERMINAL = "the passphrase, read at the controlling terminal"


def by_passphrase(revision: int) -> None:
    """Said by the command, once the passphrase it read has been checked: which
    passphrase it was. What this task then decides is recorded as confirmed by it."""
    _CONFIRMED.set(revision)


async def confirmation_in(connection: Connection) -> dict[str, Any] | None:
    """What a receipt records of how its decision was confirmed, read in the transaction
    that decides. Refuses if the passphrase is no longer the one that was checked."""
    revision = _CONFIRMED.get()
    if revision is None:
        return None
    row = await (
        await connection.execute("SELECT passphrase_revision FROM installation FOR SHARE")
    ).fetchone()
    if row is None or row["passphrase_revision"] != revision:
        raise ActionRefused(
            "the passphrase was changed after it was checked for this decision: nothing was "
            "decided. Decide again, with the passphrase as it is now"
        )
    return {"method": AT_THE_TERMINAL, "passphrase_revision": revision}
