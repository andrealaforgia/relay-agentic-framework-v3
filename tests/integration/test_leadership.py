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


class Passes:
    """Stands where the pass does, and says when it was run and for which project."""

    def __init__(self) -> None:
        self.run: list[str] = []
        self.starting: list[tuple[str, bool]] = []
        self.failing: set[str] = set()
        self.again = asyncio.Event()

    async def start(self) -> None:
        return None

    async def reconcile(self, project_id: str, *, starting: bool = False) -> None:
        self.run.append(project_id)
        self.starting.append((project_id, starting))
        self.again.set()
        if project_id in self.failing:
            raise RuntimeError(f"the repository of {project_id} cannot be reached")


DRAINED: list[str] = []


async def nothing_to_wait_for(within: float = 30.0) -> None:
    DRAINED.append("drained")


async def serving(database: AsyncConnectionPool, broker: Any, every: float) -> Any:
    from types import SimpleNamespace

    from relay.application.leadership import installation_of
    from relay.transport.streams import Streams

    async with database.connection() as connection:
        installation = await installation_of(connection)
        await connection.execute(
            "INSERT INTO projects (project_id, repository, trunk) VALUES ('alpha', 'r', 'main')"
        )
    return SimpleNamespace(
        pool=database,
        installation=installation,
        integration=SimpleNamespace(drain=nothing_to_wait_for),
        features=Passes(),
        streams=Streams(broker, prefix=installation),
        invocations=Invocations(database),
    )


@pytest.mark.parametrize("every, woken", [(600.0, True), (0.2, False)])
async def test_the_control_plane_runs_a_projects_pass_when_woken_and_on_its_timer_when_not(
    database: AsyncConnectionPool, broker: Any, every: float, woken: bool
) -> None:
    """A wake is what makes work taken up soon. A wake that is lost costs only time:
    the timer runs the pass anyway."""
    from relay import processes
    from relay.runtime import wake

    relay = await serving(database, broker, every)
    stop = asyncio.Event()

    async def the_control_plane() -> None:
        await processes.control_plane(relay, broker, stop, every=every)

    running = asyncio.create_task(the_control_plane(), context=contextvars.copy_context())
    try:
        async with asyncio.timeout(20):
            await relay.features.again.wait()  # the first pass, at start
            relay.features.again.clear()
            if woken:
                await wake(database, relay.installation, "alpha")
            await relay.features.again.wait()
        assert relay.features.run == ["alpha", "alpha"]
        DRAINED.clear()
    finally:
        stop.set()
        await running
        await broker.delete(processes._marker(relay.installation))
    # As it stops, it waits for whatever Git has said that is still being written down.
    assert DRAINED == ["drained"]


async def test_only_the_control_plane_is_assembled_with_a_pass_that_runs(
    database: AsyncConnectionPool, broker: Any, tmp_path: Path, runner_image: str
) -> None:
    """3C-1.1: whatever a worker or a command calls that would run a pass, a decision's
    reconfirmations among them, wakes the control plane. And an installation that has
    not been told where its work is kept says so."""
    from relay.application.features import Features
    from relay.runtime import Elsewhere, NotConfigured, assemble, configure

    with pytest.raises(NotConfigured, match="relay init"):
        await assemble(database, broker)
    await configure(
        database, workspaces=str(tmp_path / "w"), mirrors=str(tmp_path / "m"), image=runner_image
    )
    try:
        elsewhere = await assemble(database, broker)
        leading = await assemble(database, broker, control_plane=True)

        assert type(elsewhere.features) is Elsewhere
        assert elsewhere.features.installation == elsewhere.installation
        assert type(leading.features) is Features
        assert elsewhere.decisions._features is elsewhere.features
    finally:
        from relay.git.checkout import name_attempts_under

        name_attempts_under("")


async def test_one_projects_start_that_fails_holds_up_no_other_project_and_is_tried_again(
    database: AsyncConnectionPool, broker: Any
) -> None:
    """3C-S1A-1.4: start-up cleanup is each project's own first pass. One whose repository
    cannot be reached is tried again as a start, and the others are served meanwhile."""
    from relay import processes

    relay = await serving(database, broker, 0.2)
    async with database.connection() as connection:
        await connection.execute(
            "INSERT INTO projects (project_id, repository, trunk) VALUES ('beta', 'r2', 'main')"
        )
    passes = relay.features
    passes.failing.add("alpha")
    stop = asyncio.Event()

    async def the_control_plane() -> None:
        await processes.control_plane(relay, broker, stop, every=0.2)

    running = asyncio.create_task(the_control_plane(), context=contextvars.copy_context())
    try:
        async with asyncio.timeout(20):
            while passes.starting.count(("alpha", True)) < 2 or (
                ("beta", False) not in passes.starting
            ):
                await asyncio.sleep(0.05)
            assert ("alpha", False) not in passes.starting
            passes.failing.clear()
            while ("alpha", False) not in passes.starting:
                await asyncio.sleep(0.05)
        assert passes.starting.count(("beta", True)) == 1
        assert await broker.get(processes._marker(relay.installation)) is not None
    finally:
        stop.set()
        await running
        await broker.delete(processes._marker(relay.installation))


@pytest.mark.parametrize("fails", ["once", "always"])
async def test_a_control_plane_that_cannot_listen_tries_again_and_does_not_sit_on_the_installation(
    database: AsyncConnectionPool, broker: Any, monkeypatch: pytest.MonkeyPatch, fails: str
) -> None:
    """3C-S1A-1.8: a listener that fails is started again, and wakes are heard after. One
    that never starts does not leave the process holding the installation and deaf to
    being told to stop: it gives the installation up and says why."""
    from relay import processes
    from relay.runtime import wake

    relay = await serving(database, broker, 600.0)
    real, asked = processes.channel, 0

    def channel(installation: str) -> str:
        nonlocal asked
        asked += 1
        if fails == "always" or asked == 1:
            raise OSError("no connection to listen on")
        return real(installation)

    monkeypatch.setattr(processes, "channel", channel)
    stop = asyncio.Event()

    async def the_control_plane() -> None:
        await processes.control_plane(
            relay, broker, stop, every=600.0, listening_within=1.0, listening_again_after=0.05
        )

    running = asyncio.create_task(the_control_plane(), context=contextvars.copy_context())
    try:
        if fails == "always":
            with pytest.raises(processes.CannotListen):
                async with asyncio.timeout(20):
                    await running
            assert relay.features.run == []

            async def another() -> None:
                await (await Leadership.take(database)).release()

            await as_a_process(another)  # the installation was given up
        else:
            async with asyncio.timeout(20):
                await relay.features.again.wait()
                relay.features.again.clear()
                await wake(database, relay.installation, "alpha")
                await relay.features.again.wait()
            assert relay.features.run == ["alpha", "alpha"]
    finally:
        stop.set()
        await asyncio.gather(running, return_exceptions=True)
        await broker.delete(processes._marker(relay.installation))


async def test_a_control_plane_whose_term_is_over_publishes_nothing_and_marks_nothing_published(
    database: AsyncConnectionPool, broker: Any, namespace: str
) -> None:
    """3C-S1A-1.3: it may be on another Redis than its successor. A message it appended
    there and marked published would never be published where it is read."""
    from relay.persistence.outbox import OutboxPublisher
    from relay.transport.streams import Streams

    await serving(database, broker, 1.0)
    async with database.connection() as connection:
        await connection.execute(
            "INSERT INTO outbox (message_id, project_id, stream, envelope) "
            "VALUES (gen_random_uuid(), 'alpha', 'relay:alpha:cmd:qa', '{}')"
        )
    its_redis = Streams(broker, prefix=namespace)
    refused: list[object] = []

    async def the_old_one() -> None:
        mine = await Leadership.take(database)
        await mine.drop_connection()

        async def the_new_one() -> None:
            await (await Leadership.take(database)).release()

        await as_a_process(the_new_one)
        try:
            await OutboxPublisher(database, its_redis).publish_pending()
        except NotLeader as over:
            refused.append(over)

    await as_a_process(the_old_one)

    assert len(refused) == 1
    async with database.connection() as connection:
        row = await (await connection.execute("SELECT published_at FROM outbox")).fetchone()
    assert row is not None and row["published_at"] is None
    assert [key async for key in broker.scan_iter(f"{namespace}:*")] == []
    assert await OutboxPublisher(database, its_redis).publish_pending() == 1  # one with no term


async def test_a_worker_that_lost_the_connection_its_lock_is_on_stops_and_says_so(
    database: AsyncConnectionPool, broker: Any
) -> None:
    """3C-S1A-1.6: its pool and its Redis still work, and to everyone else it is gone: it
    would be refused every claim and acknowledge every command, for ever. It stops."""
    from types import SimpleNamespace

    from relay import processes
    from relay.application.leadership import installation_of

    async with database.connection() as connection:
        installation = await installation_of(connection)
    relay: Any = SimpleNamespace(
        pool=database, installation=installation, claims=None, invocations=None,
        tasks={"qa": {}}, boundaries=None, streams=None,
    )  # fmt: skip
    stop = asyncio.Event()
    serving_qa = asyncio.create_task(
        processes.worker(relay, broker, "qa", None, dict, stop, idle=0.05)  # type: ignore[arg-type]
    )
    try:
        async with asyncio.timeout(20):
            while not (
                keys := await column_of(database, "SELECT lock_key FROM workers WHERE role = 'qa'")
            ):
                await asyncio.sleep(0.05)
            key = int(keys[0]) & 0xFFFFFFFFFFFFFFFF
            ended = await column_of(
                database,
                "SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype = 'advisory' "
                f"AND granted AND objsubid = 1 AND classid = {key >> 32} "
                f"AND objid = {key & 0xFFFFFFFF}",
            )
            assert ended == [True]
            with pytest.raises(processes.LostIdentity, match="Start it again"):
                await serving_qa
    finally:
        stop.set()
        await asyncio.gather(serving_qa, return_exceptions=True)


async def column_of(database: AsyncConnectionPool, query: str) -> list[Any]:
    async with database.connection() as connection:
        rows = await (await connection.execute(query)).fetchall()  # type: ignore[arg-type]
    return [next(iter(row.values())) for row in rows]


async def test_a_worker_process_asks_before_every_call_whether_it_is_stopping_and_whose_redis_it_is_on(
    database: AsyncConnectionPool, broker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3C-S1A-1.5: what the process hands its worker to ask before each call. Whose Redis
    this is is asked each time, not remembered from thirty seconds ago."""
    from types import SimpleNamespace

    from relay import processes
    from relay.application.leadership import installation_of

    async with database.connection() as connection:
        installation = await installation_of(connection)
    relay: Any = SimpleNamespace(
        pool=database, installation=installation, claims=None, invocations=None,
        tasks={"qa": {}}, boundaries=None, streams=None,
    )  # fmt: skip
    handed: dict[str, Any] = {}

    class Recorded:
        def __init__(self, *_: Any, **asked: Any) -> None:
            handed.update(asked)

        async def serve_once(self, *_: Any) -> list[str]:
            return []

    monkeypatch.setattr(processes, "Worker", Recorded)
    stop = asyncio.Event()
    serving_qa = asyncio.create_task(
        processes.worker(relay, broker, "qa", None, dict, stop, idle=0.05)  # type: ignore[arg-type]
    )
    try:
        async with asyncio.timeout(20):
            while "may_launch" not in handed:
                await asyncio.sleep(0.02)
        may_launch = handed["may_launch"]
        assert "none has been started" in await may_launch()

        async def led() -> int:
            mine = await Leadership.take(database)
            await mine.release()
            return mine.term

        first = await as_a_process(led)
        await broker.set(processes._marker(installation), first)
        assert await may_launch() is None
        await as_a_process(led)  # a control plane started since, on another Redis
        assert "another Redis" in await may_launch()
        stop.set()
        assert await may_launch() == "this worker is stopping"
    finally:
        stop.set()
        await serving_qa
        await broker.delete(processes._marker(installation))


async def test_a_control_plane_whose_term_is_over_numbers_no_attempt(building: Building) -> None:
    """3C-S1A-1.3: one of the writes a gate check in an earlier transaction cannot fence."""
    world = building.world
    worker = await building.worker(Builder(RIGHT))
    assert await worker.carry_out(await building.owed()) == "handed_on"
    async with world.database.connection() as connection:
        invocation = await (await connection.execute("SELECT * FROM invocations")).fetchone()
    assert invocation is not None

    async def the_old_one() -> None:
        mine = await Leadership.take(world.database)
        await mine.drop_connection()

        async def the_new_one() -> None:
            await (await Leadership.take(world.database)).release()

        await as_a_process(the_new_one)
        with pytest.raises(NotLeader):
            await building.task._attempt(invocation)

    await as_a_process(the_old_one)

    assert await world.column("SELECT count(*) FROM build_attempts") == [0]


async def test_a_control_plane_told_to_stop_before_it_can_listen_stops(
    database: AsyncConnectionPool, broker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3C-S1A-1.8: as Astra found it, the process stayed blocked with its stop set."""
    from relay import processes

    relay = await serving(database, broker, 600.0)

    def channel(installation: str) -> str:
        raise OSError("no connection to listen on")

    monkeypatch.setattr(processes, "channel", channel)
    stop = asyncio.Event()

    async def the_control_plane() -> None:
        await processes.control_plane(relay, broker, stop, every=600.0, listening_within=600.0)

    running = asyncio.create_task(the_control_plane(), context=contextvars.copy_context())
    await asyncio.sleep(0.3)
    stop.set()

    async with asyncio.timeout(10):
        await running  # and no complaint that it could not listen: it was told to stop
    assert relay.features.run == []


async def test_a_control_plane_whose_write_arrives_late_does_not_put_its_term_back_in_redis(
    broker: Any, namespace: str
) -> None:
    """3C-S1A-2.7: its successor's term is there. Every worker would refuse the right
    Redis until the successor wrote again."""
    from relay import processes

    try:
        assert await processes.mark(broker, namespace, 1, 60)
        assert await processes.mark(broker, namespace, 2, 60)
        await broker.expire(processes._marker(namespace), 5)

        assert not await processes.mark(broker, namespace, 1, 60)

        assert await broker.get(processes._marker(namespace)) == "2"
        assert await broker.ttl(processes._marker(namespace)) <= 5  # nor kept alive by it
        assert await processes.mark(broker, namespace, 2, 60)  # its own, renewed
        assert await broker.ttl(processes._marker(namespace)) > 5
    finally:
        await broker.delete(processes._marker(namespace))


class Unreachable:
    """Redis, gone for as long as the test says."""

    def __init__(self, broker: Any) -> None:
        self._broker, self.gone = broker, True

    def __getattr__(self, name: str) -> Any:
        import redis.exceptions

        real = getattr(self._broker, name)
        if not callable(real):
            return real

        async def asked(*arguments: Any, **more: Any) -> Any:
            if self.gone:
                raise redis.exceptions.ConnectionError("connection refused")
            return await real(*arguments, **more)

        return asked


async def test_processes_outlast_a_redis_that_goes_away_and_find_it_when_it_is_back(
    database: AsyncConnectionPool, broker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3C-S1A-2.6: neither process ends because Redis cannot be reached. The control plane
    writes its marker when it can again; the worker launches nothing until it has read
    it, and says why."""
    from relay import processes

    relay = await serving(database, broker, 0.2)
    relay.tasks, relay.claims, relay.boundaries = {"qa": {}}, None, None
    redis_now = Unreachable(broker)
    handed: dict[str, Any] = {}

    class Recorded:
        def __init__(self, *_: Any, **asked: Any) -> None:
            handed.update(asked)

        async def serve_once(self, *_: Any) -> list[str]:
            handed["served"] = handed.get("served", 0) + 1
            return []

    monkeypatch.setattr(processes, "Worker", Recorded)
    stop = asyncio.Event()

    async def the_control_plane() -> None:
        await processes.control_plane(relay, redis_now, stop, every=0.2)

    leading = asyncio.create_task(the_control_plane(), context=contextvars.copy_context())
    serving_qa = asyncio.create_task(
        processes.worker(relay, redis_now, "qa", None, dict, stop, idle=0.05)  # type: ignore[arg-type]
    )
    try:
        async with asyncio.timeout(20):
            while "may_launch" not in handed or len(relay.features.run) < 3:
                await asyncio.sleep(0.05)
            assert "cannot be reached" in await handed["may_launch"]()
            assert "served" not in handed
            assert not leading.done() and not serving_qa.done()
            redis_now.gone = False
            while "served" not in handed:
                await asyncio.sleep(0.05)
        assert await handed["may_launch"]() is None
    finally:
        stop.set()
        await asyncio.gather(leading, serving_qa, return_exceptions=True)
        await broker.delete(processes._marker(relay.installation))
    assert leading.exception() is None and serving_qa.exception() is None


async def test_a_project_a_worker_cannot_serve_does_not_take_the_worker_from_another(
    database: AsyncConnectionPool, broker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3C-S1A-2.5: the repository of one project cannot be reached while its call is being
    prepared. The same process serves the other, and comes back to the first."""
    from relay import processes
    from relay.git.checkout import RemoteUnavailable

    relay = await serving(database, broker, 1.0)
    relay.tasks, relay.claims, relay.boundaries = {"analyst": {}}, None, None
    async with database.connection() as connection:
        await connection.execute(
            "INSERT INTO projects (project_id, repository, trunk) VALUES ('beta', 'r2', 'main')"
        )
    served: list[str] = []

    class OneRepositoryGone:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def serve_once(self, _: Any, project_id: str) -> list[str]:
            served.append(project_id)
            if project_id == "alpha":
                raise RemoteUnavailable("no route to host")
            return ["applied"]

    monkeypatch.setattr(processes, "Worker", OneRepositoryGone)

    async def led() -> int:
        mine = await Leadership.take(database)
        await mine.release()
        return mine.term

    await broker.set(processes._marker(relay.installation), await as_a_process(led))
    stop = asyncio.Event()
    serving_analyst = asyncio.create_task(
        processes.worker(relay, broker, "analyst", None, dict, stop, idle=0.05)  # type: ignore[arg-type]
    )
    try:
        async with asyncio.timeout(20):
            while served.count("alpha") < 2 or served.count("beta") < 2:
                await asyncio.sleep(0.05)
        assert not serving_analyst.done()
    finally:
        stop.set()
        await serving_analyst
        await broker.delete(processes._marker(relay.installation))


async def test_a_statement_of_policy_cannot_be_changed_under_a_call_being_admitted(
    database: AsyncConnectionPool,
) -> None:
    """3C-S1A-2.8: admission reads the statement for share, and holds it until the call is
    on record. Whoever restates the policy waits for that; a call admitted after reads
    the new statement."""
    import psycopg

    from relay.runtime import stated_policy

    async with database.connection() as admitting, admitting.transaction():
        assert await stated_policy(admitting) is None
        async with database.connection() as restating:
            await restating.execute("SET lock_timeout = '300ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                await restating.execute(
                    "UPDATE installation SET policy = jsonb_build_object('statement', 'new')"
                )
            await restating.rollback()
            await restating.execute("RESET lock_timeout")
    async with database.connection() as restating:
        await restating.execute(
            "UPDATE installation SET policy = jsonb_build_object('statement', 'new')"
        )
    async with database.connection() as admitting:
        assert await stated_policy(admitting) == "new"


async def test_a_call_is_admitted_against_the_term_as_it_is_held_through_admission(
    database: AsyncConnectionPool, broker: Any
) -> None:
    """3C-S1A-2.3: asked with admission's connection, whose Redis this is is settled
    against a term no control plane can open past until that transaction ends."""
    import psycopg

    from relay.processes import _marker, transport_is_the_control_planes

    relay = await serving(database, broker, 1.0)

    async def led() -> int:
        mine = await Leadership.take(database)
        await mine.release()
        return mine.term

    try:
        await broker.set(_marker(relay.installation), await as_a_process(led))
        async with database.connection() as admitting, admitting.transaction():
            assert await transport_is_the_control_planes(relay, broker, admitting) == ""
            async with database.connection() as taking:
                await taking.execute("SET lock_timeout = '300ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    await taking.execute("UPDATE installation SET term = term + 1")
                await taking.rollback()
                await taking.execute("RESET lock_timeout")
    finally:
        await broker.delete(_marker(relay.installation))


async def test_a_control_plane_that_cannot_write_an_answer_down_still_gives_up_the_installation(
    database: AsyncConnectionPool, broker: Any
) -> None:
    """3C-S1A-3.3: the drain's deadline passes, it is said which send, and the stop goes
    on: the installation is another's to take."""
    from relay import processes
    from relay.application.integration import AnswersNotRecorded

    relay = await serving(database, broker, 0.2)
    waited: list[float] = []

    async def cannot(within: float) -> None:
        waited.append(within)
        raise AnswersNotRecorded("could not be put on record before stopping: heard for send X")

    relay.integration.drain = cannot
    stop = asyncio.Event()

    async def the_control_plane() -> None:
        await processes.control_plane(relay, broker, stop, every=0.2, draining_within=0.4)

    running = asyncio.create_task(the_control_plane(), context=contextvars.copy_context())
    try:
        async with asyncio.timeout(20):
            await relay.features.again.wait()
    finally:
        stop.set()
        await running  # and it ends without raising
        await broker.delete(processes._marker(relay.installation))

    assert waited == [0.4]

    async def another() -> None:
        await (await Leadership.take(database)).release()

    await as_a_process(another)


async def test_a_worker_told_to_stop_while_it_asks_whose_redis_it_is_on_may_launch_nothing(
    database: AsyncConnectionPool, broker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3C-S1A-3.4: the stop is read last, after everything that waits."""
    from types import SimpleNamespace

    from relay import processes
    from relay.application.leadership import installation_of

    async with database.connection() as connection:
        installation = await installation_of(connection)
    relay: Any = SimpleNamespace(
        pool=database, installation=installation, claims=None, invocations=None,
        tasks={"qa": {}}, boundaries=None, streams=None,
    )  # fmt: skip
    handed: dict[str, Any] = {}

    class Recorded:
        def __init__(self, *_: Any, **asked: Any) -> None:
            handed.update(asked)

        async def serve_once(self, *_: Any) -> list[str]:
            return []

    monkeypatch.setattr(processes, "Worker", Recorded)
    stop = asyncio.Event()
    told_during_the_read = [False]

    class ToldToStopAsItReads:
        def __getattr__(self, name: str) -> Any:
            return getattr(broker, name)

        async def get(self, key: str) -> Any:
            marked = await broker.get(key)
            if told_during_the_read[0]:
                stop.set()
            return marked

    async def led() -> int:
        mine = await Leadership.take(database)
        await mine.release()
        return mine.term

    await broker.set(processes._marker(installation), await as_a_process(led))
    serving_qa = asyncio.create_task(
        processes.worker(relay, ToldToStopAsItReads(), "qa", None, dict, stop, idle=0.05)  # type: ignore[arg-type]
    )
    try:
        async with asyncio.timeout(20):
            while "may_launch" not in handed:
                await asyncio.sleep(0.02)
        assert await handed["may_launch"]() is None
        assert handed["stopping"]() is False
        told_during_the_read[0] = True
        assert await handed["may_launch"]() == "this worker is stopping"
        assert handed["stopping"]() is True
    finally:
        stop.set()
        await serving_qa
        await broker.delete(processes._marker(installation))
