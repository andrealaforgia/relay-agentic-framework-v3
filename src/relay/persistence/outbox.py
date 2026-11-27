"""Transactional outbox: a message is committed with its cause and published afterwards."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from relay.application.leadership import require_term
from relay.persistence.database import Connection, Pool
from relay.transport.envelope import Envelope
from relay.transport.streams import Streams


@dataclass(frozen=True)
class OutboxMessage:
    message_id: uuid.UUID
    stream: str
    envelope: dict[str, Any]


async def enqueue(connection: Connection, stream: str, envelope: Envelope) -> None:
    await connection.execute(
        "INSERT INTO outbox (message_id, project_id, stream, envelope) VALUES (%s, %s, %s, %s)",
        (envelope.message_id, envelope.project_id, stream, Jsonb(envelope.to_dict())),
    )


async def pending_messages(pool: Pool) -> list[OutboxMessage]:
    async with pool.connection() as connection:
        rows = await (
            await connection.execute(
                "SELECT message_id, stream, envelope FROM outbox "
                "WHERE published_at IS NULL ORDER BY position"
            )
        ).fetchall()
    return [OutboxMessage(**row) for row in rows]


# A command that announces work in a stopped scope (its feature paused or cancelled, or a
# question open over it) is held back. One that
# was selected before the closure committed may still go out; it cannot be granted.
_FOR_A_CLOSED_FEATURE = (
    "SELECT 1 FROM assignments a WHERE a.command_message_id = o.message_id "
    "AND standing(a.project_id, a.feature_id, a.story_id) IS NOT NULL"
)


class OutboxPublisher:
    """Publishes then marks sent. A crash in between republishes the same message id."""

    def __init__(self, pool: Pool, streams: Streams) -> None:
        self._pool = pool
        self._streams = streams

    async def publish_pending(self) -> int:
        published = 0
        while await self._publish_next():
            published += 1
        return published

    async def _publish_next(self) -> bool:
        async with self._pool.connection() as connection, connection.transaction():
            # A control plane that has been replaced may be on another Redis than its
            # successor. It appends nothing and marks nothing published: the share lock
            # this takes holds a new term off until the mark has committed.
            await require_term(connection)
            row = await (
                await connection.execute(
                    "SELECT position, message_id, stream, envelope FROM outbox o "
                    f"WHERE published_at IS NULL AND NOT EXISTS ({_FOR_A_CLOSED_FEATURE}) "
                    "ORDER BY position LIMIT 1 FOR UPDATE OF o SKIP LOCKED"
                )
            ).fetchone()
            if row is None:
                return False
            await self._streams.append(row["stream"], row["message_id"], row["envelope"])
            await connection.execute(
                "UPDATE outbox SET published_at = now() WHERE position = %s", (row["position"],)
            )
            return True
