"""Durable receipts make an at-least-once delivery produce one effect."""

from __future__ import annotations

import uuid

from relay.persistence.database import Connection


async def act_once(connection: Connection, consumer: str, message_id: uuid.UUID) -> bool:
    """True the first time this consumer sees the message. Call inside the effect's transaction."""
    row = await (
        await connection.execute(
            "INSERT INTO receipts (consumer, message_id) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING RETURNING message_id",
            (consumer, message_id),
        )
    ).fetchone()
    return row is not None
