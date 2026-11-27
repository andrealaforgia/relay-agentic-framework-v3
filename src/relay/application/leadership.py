"""Which control plane may act, and how one that has been replaced is stopped.

One control plane runs per installation. It holds a PostgreSQL advisory lock on a
connection of its own for as long as it lives, so a second one refuses to start. But a
lock says who may start; it does not stop a process that lost its lock connection and
has not noticed from finishing what it was doing. So each control plane that takes the
lock opens a new term, and every transaction of a control plane that changes workflow
state reads the term and is refused if it is not its own.

The term a task acts under is carried in its context, set once by the process that is
the control plane. Workers and commands have none, and are not fenced: what they may do
is decided by grants.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from relay.persistence.database import Connection, Pool

_TERM: ContextVar[int | None] = ContextVar("relay_control_plane_term", default=None)
# One lock for the installation's control plane.
_LOCK_KEY = 0x52454C4159  # "RELAY"


class NotLeader(Exception):
    """This control plane's term is over: another has taken its place."""


class AlreadyLed(Exception):
    """Another control plane holds this installation."""


def term() -> int | None:
    """The term this task acts under; None for a worker or a command."""
    return _TERM.get()


async def require_term(connection: Connection) -> None:
    """Refuse a control plane that is no longer the one. Read for share, in the
    transaction that is about to change something: a new term cannot open until this
    transaction has ended, and this one sees whatever term opened before it began."""
    mine = _TERM.get()
    if mine is None:
        return
    row = await (await connection.execute("SELECT term FROM installation FOR SHARE")).fetchone()
    assert row is not None
    if row["term"] != mine:
        raise NotLeader(f"term {mine} is over: the installation is at term {row['term']}")


async def current_term(connection: Connection) -> int:
    row = await (await connection.execute("SELECT term FROM installation")).fetchone()
    assert row is not None
    return int(row["term"])


async def installation_of(connection: Connection) -> str:
    row = await (await connection.execute("SELECT installation_id FROM installation")).fetchone()
    assert row is not None
    return str(row["installation_id"])


@dataclass
class Leadership:
    """A control plane's hold on the installation: its lock, and its term."""

    term: int
    _pool: Pool
    _held: Connection | None

    @classmethod
    async def take(cls, pool: Pool) -> Leadership:
        """Take the installation, or raise `AlreadyLed`. Opens a new term and makes it
        the term of the calling task and of every task it starts from here on."""
        held = await pool.getconn()
        await held.set_autocommit(True)
        took = await (
            await held.execute("SELECT pg_try_advisory_lock(%s) AS took", (_LOCK_KEY,))
        ).fetchone()
        if took is None or not took["took"]:
            await held.set_autocommit(False)
            await pool.putconn(held)
            raise AlreadyLed("another control plane is running against this database")
        opened = await (
            await held.execute("UPDATE installation SET term = term + 1 RETURNING term")
        ).fetchone()
        assert opened is not None
        _TERM.set(int(opened["term"]))
        return cls(int(opened["term"]), pool, held)

    async def held(self) -> bool:
        """Whether the lock connection still answers. A control plane that finds it does
        not stops scheduling at once; one that has not looked yet is refused at its
        next transaction, by the term."""
        if self._held is None or self._held.closed:
            return False
        try:
            await self._held.execute("SELECT 1")
        except Exception:  # noqa: BLE001 - whatever is wrong with it, it is not held
            return False
        return True

    async def drop_connection(self) -> None:
        """Lose the lock as a broken connection would, leaving the process alive and its
        term in its context: what a test of a replaced control plane needs."""
        if self._held is not None:
            held, self._held = self._held, None
            await held.close()

    async def release(self) -> None:
        if self._held is None:
            return
        held, self._held = self._held, None
        if not held.closed:
            await held.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
            await held.set_autocommit(False)
        await self._pool.putconn(held)
