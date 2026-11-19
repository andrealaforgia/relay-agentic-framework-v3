"""Scenario 11 (duplicate command) and the outbox crash windows. Redis delivers at least
once; a durable receipt committed with the effect makes the consumer act once. The effect
here is a row in PostgreSQL, so it survives the consumer that made it."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import redis.asyncio as redis
from psycopg_pool import AsyncConnectionPool

from relay.persistence.outbox import OutboxPublisher, enqueue, pending_messages
from relay.persistence.receipts import act_once
from relay.transport.envelope import Envelope, MessageKind
from relay.transport.streams import Streams

PROJECT = "vat-calculator"
STREAM = f"relay:{PROJECT}:cmd:builder"


class DiesAfterRedisAccepts(Streams):
    async def append(self, stream: str, message_id: uuid.UUID, envelope: dict[str, Any]) -> None:
        await super().append(stream, message_id, envelope)
        raise RuntimeError("publisher died after Redis accepted the message")


class DiesBeforeRedisAccepts(Streams):
    async def append(self, stream: str, message_id: uuid.UUID, envelope: dict[str, Any]) -> None:
        raise ConnectionError("broker unreachable")


class ConsumerCrashed(Exception):
    pass


@pytest.fixture
async def outbox(database: AsyncConnectionPool) -> AsyncConnectionPool:
    """One committed build request, and a table for the consumer's durable effect."""
    async with database.connection() as connection, connection.transaction():
        await connection.execute(
            "INSERT INTO projects (project_id, repository) VALUES (%s, %s)", (PROJECT, "unused")
        )
        await connection.execute("CREATE TABLE builds_started (message_id uuid NOT NULL)")
        await enqueue(connection, STREAM, build_request())
    return database


def build_request() -> Envelope:
    return Envelope(
        message_kind=MessageKind.COMMAND,
        event_type="build.requested",
        project_id=PROJECT,
        behaviour_id="B1",
        sender="relay/control-plane",
        recipient="builder",
        idempotency_key=f"build:{PROJECT}:B1:{uuid.uuid4()}",
        correlation_id=uuid.uuid7(),
    )


async def consume(
    streams: Streams,
    database: AsyncConnectionPool,
    consumer: str,
    *,
    crash_before_commit: bool = False,
    crash_before_acknowledging: bool = False,
    reclaim: bool = False,
) -> None:
    """A fresh consumer each call: nothing is remembered between calls except PostgreSQL."""
    if reclaim:
        deliveries = await streams.claim_abandoned(
            STREAM, group="builder", consumer=consumer, idle_for_ms=0
        )
    else:
        deliveries = await streams.read(STREAM, group="builder", consumer=consumer)
    for delivery in deliveries:
        async with database.connection() as connection, connection.transaction():
            if await act_once(connection, "builder", delivery.message_id):
                await connection.execute(
                    "INSERT INTO builds_started (message_id) VALUES (%s)", (delivery.message_id,)
                )
            if crash_before_commit:
                raise ConsumerCrashed
        if crash_before_acknowledging:
            raise ConsumerCrashed
        await streams.acknowledge(STREAM, "builder", delivery)


async def builds_started(database: AsyncConnectionPool) -> int:
    async with database.connection() as connection:
        row = await (
            await connection.execute("SELECT count(*) AS n FROM builds_started")
        ).fetchone()
    assert row is not None
    return int(row["n"])


async def test_a_committed_message_reaches_its_stream_and_takes_effect_once(
    outbox: AsyncConnectionPool, broker: redis.Redis, namespace: str
) -> None:
    streams = Streams(broker, prefix=namespace)

    assert await OutboxPublisher(outbox, streams).publish_pending() == 1
    await consume(streams, outbox, "worker-1")

    assert await pending_messages(outbox) == []
    assert await builds_started(outbox) == 1


async def test_a_broker_that_refuses_the_message_leaves_it_pending_for_the_next_attempt(
    outbox: AsyncConnectionPool, broker: redis.Redis, namespace: str
) -> None:
    with pytest.raises(ConnectionError):
        await OutboxPublisher(outbox, DiesBeforeRedisAccepts(broker, namespace)).publish_pending()
    assert len(await pending_messages(outbox)) == 1

    streams = Streams(broker, prefix=namespace)
    await OutboxPublisher(outbox, streams).publish_pending()
    await consume(streams, outbox, "worker-1")

    assert await builds_started(outbox) == 1


async def test_a_publisher_crash_after_redis_accepts_republishes_but_the_effect_happens_once(
    outbox: AsyncConnectionPool, broker: redis.Redis, namespace: str
) -> None:
    with pytest.raises(RuntimeError):
        await OutboxPublisher(outbox, DiesAfterRedisAccepts(broker, namespace)).publish_pending()
    assert len(await pending_messages(outbox)) == 1

    streams = Streams(broker, prefix=namespace)
    await consume(streams, outbox, "worker-1")
    await OutboxPublisher(outbox, streams).publish_pending()
    await consume(streams, outbox, "worker-2")

    assert await pending_messages(outbox) == []
    assert await builds_started(outbox) == 1


async def test_a_consumer_crash_before_commit_loses_nothing(
    outbox: AsyncConnectionPool, broker: redis.Redis, namespace: str
) -> None:
    streams = Streams(broker, prefix=namespace)
    await OutboxPublisher(outbox, streams).publish_pending()

    with pytest.raises(ConsumerCrashed):
        await consume(streams, outbox, "worker-1", crash_before_commit=True)
    assert await builds_started(outbox) == 0

    await consume(streams, outbox, "worker-2", reclaim=True)

    assert await builds_started(outbox) == 1


async def test_a_consumer_crash_after_commit_but_before_acknowledging_does_not_repeat_the_effect(
    outbox: AsyncConnectionPool, broker: redis.Redis, namespace: str
) -> None:
    streams = Streams(broker, prefix=namespace)
    await OutboxPublisher(outbox, streams).publish_pending()

    with pytest.raises(ConsumerCrashed):
        await consume(streams, outbox, "worker-1", crash_before_acknowledging=True)
    assert await builds_started(outbox) == 1

    await consume(streams, outbox, "worker-2", reclaim=True)

    assert await builds_started(outbox) == 1
    assert (
        await streams.claim_abandoned(STREAM, group="builder", consumer="w3", idle_for_ms=0) == []
    )


async def test_abandoned_work_is_found_beyond_the_first_page_of_pending_entries(
    outbox: AsyncConnectionPool, broker: redis.Redis, namespace: str
) -> None:
    async with outbox.connection() as connection, connection.transaction():
        for _ in range(5):
            await enqueue(connection, STREAM, build_request())
    streams = Streams(broker, prefix=namespace)
    await OutboxPublisher(outbox, streams).publish_pending()
    received = await streams.read(STREAM, group="builder", consumer="worker-1", count=6)
    assert len(received) == 6

    claimed = await streams.claim_abandoned(
        STREAM, group="builder", consumer="worker-2", idle_for_ms=0, count=6, page_size=2
    )

    assert {delivery.message_id for delivery in claimed} == {d.message_id for d in received}
