"""The durable, append-only event ledger."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from relay.persistence.database import Connection, Pool


@dataclass(frozen=True)
class Event:
    event_id: uuid.UUID
    project_id: str
    sequence: int
    event_type: str
    subject_id: str | None
    payload: dict[str, Any]
    correlation_id: uuid.UUID | None
    causation_id: uuid.UUID | None
    recorded_at: datetime


async def append_event(
    connection: Connection,
    project_id: str,
    event_type: str,
    *,
    subject_id: str | None = None,
    payload: dict[str, Any] | None = None,
    correlation_id: uuid.UUID | None = None,
    causation_id: uuid.UUID | None = None,
) -> Event:
    """Append within the caller's transaction, so the event commits with the state it records."""
    allocated = await (
        await connection.execute(
            "UPDATE projects SET last_sequence = last_sequence + 1 "
            "WHERE project_id = %s RETURNING last_sequence",
            (project_id,),
        )
    ).fetchone()
    if allocated is None:
        raise LookupError(f"unknown project {project_id!r}")
    row = await (
        await connection.execute(
            "INSERT INTO events (event_id, project_id, sequence, event_type, subject_id, "
            "payload, correlation_id, causation_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING *",
            (
                uuid.uuid7(),
                project_id,
                allocated["last_sequence"],
                event_type,
                subject_id,
                Jsonb(payload or {}),
                correlation_id,
                causation_id,
            ),
        )
    ).fetchone()
    assert row is not None
    return Event(**row)


async def read_events(pool: Pool, project_id: str) -> list[Event]:
    async with pool.connection() as connection:
        rows = await (
            await connection.execute(
                "SELECT * FROM events WHERE project_id = %s ORDER BY sequence", (project_id,)
            )
        ).fetchall()
    return [Event(**row) for row in rows]
