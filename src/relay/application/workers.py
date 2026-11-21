"""A worker process, and how anyone else knows whether it is still alive.

A worker takes a PostgreSQL advisory lock when it starts, on a connection it keeps for
its whole life. PostgreSQL releases the lock when the process, or that connection, is
gone. So "is this worker alive" is a question put to the database, never a guess from a
clock or from another worker having started.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass, field

from relay.domain.story import Principal, PrincipalKind
from relay.persistence.database import Connection, Pool


@dataclass
class WorkerSession:
    worker_id: uuid.UUID
    role: str
    _pool: Pool
    _held: Connection | None
    # The effects this process is carrying out at this moment. One it is recorded as
    # having started and is not carrying out was cut short, and may be taken up again.
    # Each is held under a token of the call that took it, so that only that call lets
    # it go, whatever cut it short.
    running: dict[tuple[uuid.UUID, str], object] = field(default_factory=dict)

    @classmethod
    async def start(cls, pool: Pool, role: str) -> WorkerSession:
        worker_id = uuid.uuid7()
        # A connection of this worker's own, kept out of the pool until it stops.
        held = await pool.getconn()
        await held.set_autocommit(True)
        key = _lock_key(worker_id)
        await held.execute(
            "INSERT INTO workers (worker_id, role, host, pid, lock_key) VALUES (%s, %s, %s, %s, %s)",
            (worker_id, role, socket.gethostname(), os.getpid(), key),
        )
        await held.execute("SELECT pg_advisory_lock(%s)", (key,))
        return cls(worker_id, role, pool, held)

    @property
    def principal(self) -> Principal:
        return Principal(PrincipalKind.WORKER, f"{self.role}@{self.worker_id}", role=self.role)

    async def stop(self) -> None:
        """The worker ends. From here on it is, to everyone else, gone."""
        if self._held is None:
            return
        held, self._held = self._held, None
        await held.execute("SELECT pg_advisory_unlock(%s)", (_lock_key(self.worker_id),))
        await held.set_autocommit(False)
        await self._pool.putconn(held)


async def is_alive(connection: Connection, worker_id: uuid.UUID) -> bool:
    """Whether some session holds the worker's lock, read from PostgreSQL's own table of
    locks. Nothing is taken to find out: an observer that took the lock for a moment
    would itself look like the worker to the next observer."""
    key = _lock_key(worker_id) & 0xFFFFFFFFFFFFFFFF
    held = await (
        await connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND objsubid = 1 AND classid = %s::bigint::oid AND objid = %s::bigint::oid "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())) "
            "AS held",
            (key >> 32, key & 0xFFFFFFFF),
        )
    ).fetchone()
    assert held is not None
    return bool(held["held"])


def _lock_key(worker_id: uuid.UUID) -> int:
    return int.from_bytes(worker_id.bytes[8:], "big", signed=True)
