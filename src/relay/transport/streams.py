"""Redis Streams transport. Consumer groups share work among eligible workers; they do not
broadcast. Delivery is at least once, so every handler needs a durable receipt."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import redis.asyncio as redis
from redis.exceptions import ResponseError

type _Entry = tuple[str, dict[str, str]]


@dataclass(frozen=True)
class Delivery:
    entry_id: str
    message_id: uuid.UUID
    envelope: dict[str, Any]


class Streams:
    def __init__(self, client: redis.Redis, prefix: str = "") -> None:
        self._client = client
        self._prefix = prefix

    async def append(self, stream: str, message_id: uuid.UUID, envelope: dict[str, Any]) -> None:
        await self._client.xadd(
            self._key(stream), {"message_id": str(message_id), "envelope": json.dumps(envelope)}
        )

    async def read(
        self, stream: str, *, group: str, consumer: str, count: int = 10
    ) -> list[Delivery]:
        await self._ensure_group(stream, group)
        response: Any = await self._client.xreadgroup(
            group, consumer, {self._key(stream): ">"}, count=count
        )
        return [_delivery(entry) for _, entries in response for entry in entries]

    async def claim_abandoned(
        self,
        stream: str,
        *,
        group: str,
        consumer: str,
        idle_for_ms: int,
        count: int = 10,
        page_size: int = 100,
    ) -> list[Delivery]:
        """Take over deliveries another consumer received but never acknowledged."""
        await self._ensure_group(stream, group)
        claimed_entries: list[_Entry] = []
        cursor = "0-0"
        while len(claimed_entries) < count:
            # One page may hold only entries that are not idle long enough; keep scanning.
            page: Any = await self._client.xautoclaim(
                self._key(stream),
                group,
                consumer,
                min_idle_time=idle_for_ms,
                start_id=cursor,
                count=min(page_size, count - len(claimed_entries)),
            )
            cursor = page[0]
            claimed_entries += page[1]
            if cursor == "0-0":
                break
        return [_delivery(entry) for entry in claimed_entries]

    async def acknowledge(self, stream: str, group: str, delivery: Delivery) -> None:
        await self._client.xack(self._key(stream), group, delivery.entry_id)

    async def _ensure_group(self, stream: str, group: str) -> None:
        try:
            await self._client.xgroup_create(self._key(stream), group, id="0", mkstream=True)
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    def _key(self, stream: str) -> str:
        return f"{self._prefix}:{stream}" if self._prefix else stream


def _delivery(entry: _Entry) -> Delivery:
    entry_id, fields = entry
    return Delivery(
        entry_id=entry_id,
        message_id=uuid.UUID(fields["message_id"]),
        envelope=json.loads(fields["envelope"]),
    )
