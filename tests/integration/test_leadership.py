"""One control plane at a time, and one that has been replaced is stopped: by a lock on
who may start, and by a term on everything a control plane then does."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.invocations import Invocations
from relay.application.leadership import AlreadyLed, Leadership, NotLeader, current_term
from relay.application.tasks import Tasks
from relay.domain.behaviour import BehaviourState
from tests.integration.test_build_task import RIGHT, Builder, Building
from tests.integration.trunk_world import PROJECT


async def as_a_process[T](doing: Callable[[], Awaitable[T]]) -> T:
    """Run something as a process of its own would: with its own term, or none, which
    nothing it does leaves behind in the test."""
    return await asyncio.create_task(doing(), context=contextvars.copy_context())


async def test_a_second_control_plane_is_refused_while_the_first_holds_the_installation(
    database: AsyncConnectionPool,
) -> None:
    async def both() -> tuple[int, int]:
        first = await Leadership.take(database)
        with pytest.raises(AlreadyLed):
            await as_a_process(lambda: Leadership.take(database))
        await first.release()
        second = await Leadership.take(database)
        await second.release()
        return first.term, second.term

    first, second = await as_a_process(both)

    assert second == first + 1


async def test_a_control_plane_that_lost_its_lock_is_refused_at_its_next_change_of_anything(
    building: Building,
) -> None:
    """3C-1.2 and 3C-2.3: alive, with pooled connections that still work, and not yet
    aware. Its successor has opened a new term. Whatever the old one tries next, to start
    work or to end it, is refused in the transaction that would do it."""
    world = building.world
    successor: list[Leadership] = []

    async def the_old_one() -> None:
        mine = await Leadership.take(world.database)
        await mine.drop_connection()  # as a network fault would; the process lives on
        assert not await mine.held()

        async def the_new_one() -> None:
            successor.append(await Leadership.take(world.database))

        await as_a_process(the_new_one)
        with pytest.raises(NotLeader):
            await world.features.reconcile(PROJECT)
        with pytest.raises(NotLeader):
            await world.features.start()  # start-up cleanup ends work, and is fenced too
        # With nothing of a worker's to recover it changes nothing, and so has nothing to
        # be refused: the refusal is of a change, not of looking.
        await building.invocations.recover(PROJECT)

    await as_a_process(the_old_one)

    async with world.database.connection() as connection:
        assert await current_term(connection) == successor[0].term
    # Nothing of the build that was owed was touched by the one that was refused.
    assert await world.column("SELECT state FROM assignments WHERE kind = 'build'") == ["wanted"]
    await successor[0].release()


async def test_an_effect_started_under_an_earlier_term_is_taken_by_the_new_control_plane(
    building: Building,
) -> None:
    """3C-2.3: the old leader's own session still looks alive. It cannot mark the effect
    done, being refused by the term, so the effect is free: the new leader takes it, and
    what the old one then observed decides nothing."""
    world = building.world
    worker = await building.worker(Builder(RIGHT))
    assert await worker.carry_out(await building.owed()) == "handed_on"
    oracle = world.behaviours._oracle
    observe, observed = oracle.execute, 0
    first_is_observing, first_may_end = asyncio.Event(), asyncio.Event()

    async def execute(*arguments: Any) -> Any:
        nonlocal observed
        observed += 1
        mine = observed
        execution = await observe(*arguments)
        if mine == 1:
            first_is_observing.set()
            await first_may_end.wait()
        return execution

    oracle.execute = execute  # type: ignore[method-assign]
    ended: list[BaseException | None] = []

    async def the_old_one() -> None:
        mine = await Leadership.take(world.database)
        recovering = asyncio.create_task(building.invocations.recover(PROJECT))
        await first_is_observing.wait()
        await mine.drop_connection()

        async def the_new_one() -> None:
            mine = await Leadership.take(world.database)
            successor = Invocations(world.database)
            Tasks({"build": building.task, "remediate": building.task}).attach(successor)
            await successor.recover(PROJECT)
            await successor.close()
            await mine.release()

        await as_a_process(the_new_one)
        first_may_end.set()
        try:
            await recovering
            ended.append(None)
        except NotLeader as refused:
            ended.append(refused)

    await as_a_process(the_old_one)

    assert isinstance(ended[0], NotLeader)
    assert observed == 2
    assert await building.states() == ["applied"]
    assert await world.state("B1") is BehaviourState.REVIEW
    assert await world.column("SELECT count(*) FROM review_cycles") == [1]


async def test_attempts_are_named_under_their_installation_once_a_process_says_which(
    tmp_path: Path,
) -> None:
    """3C-3.3: two installations on branches of one repository do not take each other's."""
    from relay.git import checkout

    assert checkout.attempt_ref("P", "B1", 1) == "refs/relay/attempts/P/B1/1"
    checkout.name_attempts_under("01a2-installation")
    try:
        assert checkout.attempt_ref("P", "B1", 1) == "refs/relay/01a2-installation/attempts/P/B1/1"
    finally:
        checkout.name_attempts_under("")


async def test_a_worker_launches_nothing_on_a_redis_the_control_plane_is_not_using(
    database: AsyncConnectionPool, broker: Any
) -> None:
    """3C-1.4: the control plane leaves its term in the Redis it publishes to, and the
    term is on record in PostgreSQL. A worker given another Redis finds no mark, or the
    mark of an earlier term, and says so."""
    from types import SimpleNamespace

    from relay.application.leadership import installation_of
    from relay.processes import _marker, transport_is_the_control_planes

    async with database.connection() as connection:
        installation = await installation_of(connection)
    relay: Any = SimpleNamespace(pool=database, installation=installation)
    try:
        assert "none has been started" in await transport_is_the_control_planes(relay, broker)

        async def led() -> int:
            mine = await Leadership.take(database)
            await mine.release()
            return mine.term

        first = await as_a_process(led)
        assert "no mark in this Redis" in await transport_is_the_control_planes(relay, broker)
        await broker.set(_marker(installation), first)
        assert await transport_is_the_control_planes(relay, broker) == ""
        await as_a_process(led)
        assert "another Redis" in await transport_is_the_control_planes(relay, broker)
    finally:
        await broker.delete(_marker(installation))


async def test_every_process_but_the_control_plane_wakes_it_and_runs_no_pass(
    database: AsyncConnectionPool,
) -> None:
    """3C-1.1: a worker's or a command's pass is a notification to the one process that
    runs it, naming the project."""
    from relay.application.leadership import installation_of
    from relay.runtime import Elsewhere, channel

    async with database.connection() as connection:
        installation = await installation_of(connection)
    elsewhere = Elsewhere(database, None, None, completion=object())  # type: ignore[arg-type]
    elsewhere.installation = installation
    listening = await database.getconn()
    try:
        await listening.set_autocommit(True)
        await listening.execute(f"LISTEN {channel(installation)}")

        await elsewhere.reconcile("alpha")
        await elsewhere.complete_what_is_owed("beta")
        await elsewhere.start()

        heard = []
        async for notice in listening.notifies(timeout=5, stop_after=2):
            heard.append(notice.payload)
        assert heard == ["alpha", "beta"]
    finally:
        await listening.execute("UNLISTEN *")
        await listening.set_autocommit(False)
        await database.putconn(listening)
