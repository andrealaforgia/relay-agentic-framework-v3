"""Scenario 11 (duplicate command) and the outbox crash window: Redis delivers at least
once; a durable receipt makes the consumer act once."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import redis.asyncio as redis
from psycopg_pool import AsyncConnectionPool

from relay.application.behaviours import Behaviours
from relay.persistence.outbox import OutboxPublisher, pending_messages
from relay.persistence.receipts import act_once
from relay.transport.streams import Streams
from tests.integration.fixtures import CONTRACT, FAILS_AS_REGISTERED

PROJECT = "vat-calculator"


class DiesAfterRedisAccepts(Streams):
    async def append(self, stream: str, message_id: uuid.UUID, envelope: dict[str, Any]) -> None:
        await super().append(stream, message_id, envelope)
        raise RuntimeError("publisher died after Redis accepted the message")


async def one_build_request_in_the_outbox(database: AsyncConnectionPool) -> None:
    behaviours = Behaviours(database)
    await behaviours.open_project(PROJECT)
    await behaviours.propose(PROJECT, "B1", "standard VAT rate is applied")
    await behaviours.begin_test_design(PROJECT, "B1")
    await behaviours.register_acceptance_test(PROJECT, "B1", CONTRACT)
    await behaviours.record_red_run(PROJECT, "B1", FAILS_AS_REGISTERED)


async def drain(streams: Streams, database: AsyncConnectionPool, effects: list[str]) -> None:
    stream = f"relay:{PROJECT}:cmd:builder"
    for delivery in await streams.read(stream, group="builder", consumer="worker-1"):
        async with database.connection() as connection, connection.transaction():
            if await act_once(connection, "builder", delivery.message_id):
                effects.append(delivery.envelope["event_type"])
        await streams.acknowledge(stream, "builder", delivery)


async def test_a_committed_message_reaches_its_stream(
    database: AsyncConnectionPool, broker: redis.Redis, namespace: str
) -> None:
    await one_build_request_in_the_outbox(database)
    streams = Streams(broker, prefix=namespace)

    published = await OutboxPublisher(database, streams).publish_pending()

    assert published == 1
    assert await pending_messages(database) == []
    effects: list[str] = []
    await drain(streams, database, effects)
    assert effects == ["build.requested"]


async def test_a_crash_between_publish_and_mark_sent_republishes_the_same_message_once_acted(
    database: AsyncConnectionPool, broker: redis.Redis, namespace: str
) -> None:
    await one_build_request_in_the_outbox(database)
    streams = Streams(broker, prefix=namespace)
    with pytest.raises(RuntimeError):
        await OutboxPublisher(
            database, DiesAfterRedisAccepts(broker, prefix=namespace)
        ).publish_pending()
    assert len(await pending_messages(database)) == 1

    effects: list[str] = []
    await drain(streams, database, effects)
    await OutboxPublisher(database, streams).publish_pending()
    await drain(streams, database, effects)

    assert await pending_messages(database) == []
    assert effects == ["build.requested"]


async def test_an_unacknowledged_delivery_is_redelivered_and_still_acted_on_once(
    database: AsyncConnectionPool, broker: redis.Redis, namespace: str
) -> None:
    await one_build_request_in_the_outbox(database)
    streams = Streams(broker, prefix=namespace)
    await OutboxPublisher(database, streams).publish_pending()
    stream = f"relay:{PROJECT}:cmd:builder"

    [first] = await streams.read(stream, group="builder", consumer="worker-1")
    async with database.connection() as connection, connection.transaction():
        assert await act_once(connection, "builder", first.message_id)

    [again] = await streams.claim_abandoned(
        stream, group="builder", consumer="worker-2", idle_for_ms=0
    )
    async with database.connection() as connection, connection.transaction():
        assert not await act_once(connection, "builder", again.message_id)
    assert again.message_id == first.message_id
