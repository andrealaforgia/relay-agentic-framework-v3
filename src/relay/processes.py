"""The two processes that run for as long as Relay does: the control plane, and a worker.

The control plane is the only process that runs a pass: it observes reds, publishes and
observes candidates, integrates and completes. A worker makes calls to a provider and
keeps what comes back; where what it did leaves something for the control plane to do,
it says so, and does not do it.

Saying so is a notification, and a notification may be lost. So the control plane also
runs every project's pass on a timer: how soon work is taken up depends on the wake, and
whether it is taken up does not.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as redis

from relay.application.leadership import (
    Leadership,
    NotLeader,
    current_term,
    term_for_share,
)
from relay.application.worker import Provider, Worker
from relay.application.workers import WorkerSession
from relay.persistence.outbox import OutboxPublisher
from relay.runtime import Relay, channel, wake

# How long work can wait for the control plane when a wake was lost.
EVERY = 15.0
# How often a worker that found nothing looks again.
IDLE = 0.5


def _marker(installation: str) -> str:
    return f"{installation}:control"


class CannotListen(Exception):
    """The control plane could not start listening for wakes."""


class LostIdentity(Exception):
    """A worker no longer holds the lock that says it is alive."""


def _say(what: str) -> None:
    print(what, file=sys.stderr, flush=True)


async def control_plane(
    relay: Relay,
    broker: redis.Redis,
    stop: asyncio.Event,
    *,
    every: float = EVERY,
    started: Callable[[Leadership], None] | None = None,
    listening_within: float = 30.0,
    listening_again_after: float = 1.0,
) -> None:
    """Take the installation and serve it until told to stop or replaced. Raises
    `AlreadyLed` if another control plane holds it."""
    leadership = await Leadership.take(relay.pool)
    if started is not None:
        started(leadership)
    _say(f"relay: control plane of {relay.installation}, term {leadership.term}")
    woken: dict[str, asyncio.Event] = {}
    replaced = asyncio.Event()
    publisher = OutboxPublisher(relay.pool, relay.streams)
    tasks: dict[str, asyncio.Task[None]] = {}
    started_for: set[str] = set()

    async def one_project(project_id: str) -> None:
        """One pass at a time for a project. A wake that arrives during a pass is one
        more pass after it."""
        # The first pass of a project under this control plane is the one that cleans up
        # after the last: every worker of the process that ended is taken for gone. It
        # is this project's own, tried again until it has been done once, so a
        # repository that cannot be reached holds up nobody else's start.
        starting = project_id not in started_for
        while not stop.is_set() and not replaced.is_set():
            woken[project_id].clear()
            try:
                await relay.features.reconcile(project_id, starting=starting)
                starting = False
                started_for.add(project_id)
                await publisher.publish_pending()
            except NotLeader as over:
                _say(f"relay: {over}. This control plane stops.")
                replaced.set()
                return
            except Exception as failure:  # noqa: BLE001 - one project's trouble is its own
                _say(f"relay: the pass for {project_id} failed and will be run again: {failure!r}")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(woken[project_id].wait(), every)

    async def listen() -> None:
        """Hear wakes for as long as this control plane serves. A listener that fails is
        started again, and every project is then looked at: a wake may have been sent
        while nobody was listening."""
        while True:
            connection = None
            try:
                connection = await relay.pool.getconn()
                await connection.set_autocommit(True)
                await connection.execute(f"LISTEN {channel(relay.installation)}")
                if listening_now.is_set():
                    for each in woken.values():
                        each.set()
                listening_now.set()
                async for notice in connection.notifies():
                    if notice.payload in woken:
                        woken[notice.payload].set()
                    else:
                        woken_for_all.set()  # a project this process has not met yet
            except asyncio.CancelledError:
                raise
            except Exception as failure:  # noqa: BLE001 - whatever it was, listen again
                _say(f"relay: no longer listening for wakes, and trying again: {failure!r}")
            finally:
                if connection is not None:
                    with contextlib.suppress(Exception):
                        await connection.close()
                    with contextlib.suppress(Exception):
                        await relay.pool.putconn(connection)
            await asyncio.sleep(listening_again_after)

    woken_for_all, listening_now = asyncio.Event(), asyncio.Event()
    reached = True
    listening = asyncio.create_task(listen())
    try:
        # Before the first pass: a wake sent while that pass runs is then heard, and
        # makes one more pass follow it. A control plane that cannot listen does not
        # hold the installation while it waits to: it says so and stops.
        first = [asyncio.create_task(each.wait()) for each in (listening_now, stop)]
        await asyncio.wait(first, timeout=listening_within, return_when=asyncio.FIRST_COMPLETED)
        for each in first:
            each.cancel()
        if stop.is_set():
            return
        if not listening_now.is_set():
            raise CannotListen(
                f"no wake could be listened for within {listening_within:.0f} seconds"
            )
        while not stop.is_set() and not replaced.is_set():
            if not await leadership.held():
                _say("relay: the lock on the installation is lost. This control plane stops.")
                return
            # So that a worker can tell this Redis is the one the control plane uses.
            try:
                await mark(broker, relay.installation, leadership.term, int(every * 6))
                reached = True
            except (redis.RedisError, OSError) as unreachable:
                # Said once, and tried again each turn: Redis coming back finds this
                # control plane serving, and its workers waiting for the marker.
                if reached:
                    _say(f"relay: Redis cannot be reached, and is tried again: {unreachable}")
                reached = False
            async with relay.pool.connection() as connection:
                projects = await (
                    await connection.execute("SELECT project_id FROM projects ORDER BY project_id")
                ).fetchall()
            for project in projects:
                project_id = project["project_id"]
                if project_id not in tasks or tasks[project_id].done():
                    woken.setdefault(project_id, asyncio.Event())
                    tasks[project_id] = asyncio.create_task(one_project(project_id))
            with contextlib.suppress(NotLeader, redis.RedisError, OSError):
                await publisher.publish_pending()
            woken_for_all.clear()
            waiting = [asyncio.create_task(each.wait()) for each in (stop, replaced, woken_for_all)]
            await asyncio.wait(waiting, timeout=every, return_when=asyncio.FIRST_COMPLETED)
            for each in waiting:
                each.cancel()
    finally:
        listening.cancel()
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(listening, *tasks.values(), return_exceptions=True)
        # What Git has said to a send, and is being written down, is written down.
        try:
            await relay.integration.drain()
        except Exception as unwritten:  # noqa: BLE001 - said, and the stop goes on
            _say(f"relay: {unwritten}")
        await relay.invocations.close()
        await leadership.release()


# Set the marker, unless one of a later term is there: a control plane that has been
# replaced, and whose write arrives late, neither puts its term back nor keeps it alive.
_MARK = """
local there = tonumber(redis.call('GET', KEYS[1]))
if there and there > tonumber(ARGV[1]) then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""


async def mark(broker: redis.Redis, installation: str, term: int, lasting: int) -> bool:
    """Leave this control plane's term in the Redis it publishes to. False if a later
    term's is there already."""
    return bool(await broker.eval(_MARK, 1, _marker(installation), term, lasting))


async def transport_is_the_control_planes(
    relay: Relay, broker: redis.Redis, connection: Any = None
) -> str:
    """Empty if the Redis this process was given is the one the control plane is using;
    otherwise why not. The control plane leaves its term there, and the term is on
    record in PostgreSQL: a marker that is missing, or of an earlier term, is another
    Redis than the one commands are being published to."""
    if connection is None:
        async with relay.pool.connection() as asking:
            term = await current_term(asking)
    else:
        # Where a call is admitted: the term is held as it is until that transaction
        # ends, so no control plane starts, on this Redis or another, in between.
        term = await term_for_share(connection)
    try:
        marked = await broker.get(_marker(relay.installation))
    except (redis.RedisError, OSError) as unreachable:
        return f"this Redis cannot be reached, and nothing is read from it: {unreachable}"
    if term == 0:
        return "waiting for the control plane: none has been started yet"
    if marked is None:
        return (
            "waiting for the control plane: it has left no mark in this Redis. Either it "
            "is not running, or it was given another Redis than this worker"
        )
    if int(marked) < term:
        return (
            f"this Redis was last used by the control plane of term {int(marked)}, and the "
            f"installation is at term {term}: this worker has been given another Redis"
        )
    return ""


async def worker(
    relay: Relay,
    broker: redis.Redis,
    role: str,
    provider: Provider,
    runtime: Callable[[], dict[str, str]],
    stop: asyncio.Event,
    *,
    idle: float = IDLE,
    stated: Callable[[Any], Awaitable[str | None]] | None = None,
) -> None:
    """Serve one role across every project until told to stop."""
    session = await WorkerSession.start(relay.pool, role)
    _say(f"relay: worker {session.worker_id} for {role}")

    async def then(project_id: str) -> None:
        await wake(relay.pool, relay.installation, project_id)

    trouble: list[str] = ["not checked yet"]
    said: list[str] = [""]
    said_of: dict[str, str] = {}

    async def may_launch(admitting: Any = None) -> str | None:
        """Asked before every call, and before every delivery is taken up. Whose Redis
        this is is asked again each time: a call costs far more than the asking, and a
        control plane may have been started against another Redis at any moment."""
        if stop.is_set():
            return "this worker is stopping"
        if not await session.held():
            raise LostIdentity(
                f"worker {session.worker_id} has lost the connection its lock is on, and "
                "is gone to everyone else: it stops. Start it again."
            )
        trouble[0] = await transport_is_the_control_planes(relay, broker, admitting)
        if trouble[0] and trouble[0] != said[0]:
            _say(f"relay: {role} launches nothing: {trouble[0]}")
        said[0] = trouble[0]
        return trouble[0] or None

    serving: Any = Worker(
        session, relay.claims, relay.invocations, provider, relay.tasks[role], relay.boundaries,
        runtime, then=then, may_launch=may_launch, stated=stated,
    )  # fmt: skip
    try:
        while not stop.is_set():
            did = 0
            if await may_launch() is None:
                async with relay.pool.connection() as connection:
                    projects = await (
                        await connection.execute(
                            "SELECT project_id FROM projects ORDER BY project_id"
                        )
                    ).fetchall()
                for project in projects:
                    try:
                        did += len(await serving.serve_once(relay.streams, project["project_id"]))
                    except LostIdentity:
                        raise
                    except Exception as failure:  # noqa: BLE001 - one project's trouble is its own
                        # A repository that cannot be reached, a Redis that has gone:
                        # what was owed is owed still, and is announced again. The
                        # other projects are served, and this one again next turn.
                        where = project["project_id"]
                        if said_of.get(where) != repr(failure):
                            _say(
                                f"relay: {role} could not serve {where}, and will again: {failure!r}"
                            )
                        said_of[where] = repr(failure)
                    else:
                        said_of.pop(project["project_id"], None)
            if not did:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), idle)
    finally:
        await session.stop()
