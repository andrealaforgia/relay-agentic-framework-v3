"""A worker reading its role's commands. The stream is only the announcement: what is
owed is on record, so a command that comes twice buys nothing twice, and one whose
worker failed part-way is taken over."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import redis.asyncio as redis
from psycopg_pool import AsyncConnectionPool

from relay.persistence.outbox import OutboxPublisher
from relay.transport.streams import Streams
from tests.integration.decision_world import DecisionWorld
from tests.integration.fixtures import VAT_APPLIES_STANDARD_RATE, run_green
from tests.integration.story_world import STANDARD_RATE
from tests.integration.test_review_task import NOTHING_FOUND, Reviewer, Reviewing
from tests.integration.trunk_world import PROJECT


class Served(Reviewing):
    streams: Streams
    publisher: OutboxPublisher


@pytest.fixture
async def served(
    database: AsyncConnectionPool,
    broker: redis.Redis,
    namespace: str,
    tmp_path: Path,
    runner_image: str,
) -> AsyncIterator[Served]:
    world = DecisionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await world.decide(await world.roadmap(STANDARD_RATE))
    base = await world.awaiting_builder("B1")
    served = Served(world, tmp_path)
    served.streams = Streams(broker, prefix=namespace)
    served.publisher = OutboxPublisher(database, served.streams)
    world.publish_attempt("B1", {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=base)
    assert (await run_green(world.behaviours, PROJECT, "B1", world.attempts["B1"])).is_valid
    assert await world.reviews.publish_candidates() == 1
    await served.publisher.publish_pending()
    yield served
    await served.invocations.close()
    for session in served.sessions:
        await session.stop()


async def test_a_worker_carries_out_what_its_roles_stream_announces(served: Served) -> None:
    provider = Reviewer(NOTHING_FOUND)
    worker = await served.worker("qa", provider)

    assert await worker.serve_once(served.streams, PROJECT) == ["applied"]

    assert (await served.owed("qa"))["state"] == "done"
    assert (await served.owed("sentinel"))["state"] == "wanted"  # not this role's stream
    assert await worker.serve_once(served.streams, PROJECT) == []
    # Dealt with, so acknowledged: nobody finds it lying there to take over.
    another = await served.worker("qa", Reviewer())
    assert await another.serve_once(served.streams, PROJECT, abandoned_after_ms=0) == []


async def test_a_command_that_comes_again_buys_nothing_more(served: Served) -> None:
    provider = Reviewer(NOTHING_FOUND)
    worker = await served.worker("qa", provider)
    await worker.serve_once(served.streams, PROJECT)

    async with served.world.database.connection() as connection:
        await connection.execute("UPDATE outbox SET published_at = NULL")
    await served.publisher.publish_pending()
    again = await worker.serve_once(served.streams, PROJECT)

    assert len(again) == 1 and again[0].startswith(("refused", "finished"))
    assert len(provider.asked) == 1


async def test_a_command_whose_worker_failed_part_way_is_taken_over_by_another(
    served: Served,
) -> None:
    class Failing:
        async def invoke(self, *_: Any, **__: Any) -> Any:
            raise KeyboardInterrupt

    first = await served.worker("qa", Failing())
    with pytest.raises(KeyboardInterrupt):
        await first.serve_once(served.streams, PROJECT)
    await served.sessions[-1].stop()
    await served.invocations.recover(PROJECT)  # the call was cut short before launch
    provider = Reviewer(NOTHING_FOUND)
    second = await served.worker("qa", provider)

    assert await second.serve_once(served.streams, PROJECT) == []  # not yet abandoned
    assert await second.serve_once(served.streams, PROJECT, abandoned_after_ms=0) == ["applied"]

    assert len(provider.asked) == 1
    assert (await served.owed("qa"))["state"] == "done"


async def test_a_command_for_work_a_worker_does_not_do_is_set_aside(served: Served) -> None:
    worker = await served.worker("qa", Reviewer())
    worker._tasks = {}  # a worker started without the review task

    assert await worker.serve_once(served.streams, PROJECT) == ["not work this worker does"]
    assert (await served.owed("qa"))["state"] == "wanted"
