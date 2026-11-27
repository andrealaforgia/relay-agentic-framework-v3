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
from collections.abc import Callable
from typing import Any

import redis.asyncio as redis

from relay.application.leadership import Leadership, NotLeader, current_term
from relay.application.worker import Provider, Worker
from relay.application.workers import WorkerSession
from relay.persistence.outbox import OutboxPublisher
from relay.runtime import Relay, channel, wake

# How long work can wait for the control plane when a wake was lost.
EVERY = 15.0
# How often a worker that found nothing looks again, and checks whose Redis it is on.
IDLE = 0.5
CHECKING_TRANSPORT_EVERY = 30.0


def _marker(installation: str) -> str:
    return f"{installation}:control"


def _say(what: str) -> None:
    print(what, file=sys.stderr, flush=True)


async def control_plane(
    relay: Relay,
    broker: redis.Redis,
    stop: asyncio.Event,
    *,
    every: float = EVERY,
    started: Callable[[Leadership], None] | None = None,
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

    async def one_project(project_id: str) -> None:
        """One pass at a time for a project. A wake that arrives during a pass is one
        more pass after it."""
        while not stop.is_set() and not replaced.is_set():
            woken[project_id].clear()
            try:
                await relay.features.reconcile(project_id)
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
        connection = await relay.pool.getconn()
        try:
            await connection.set_autocommit(True)
            await connection.execute(f"LISTEN {channel(relay.installation)}")
            async for notice in connection.notifies():
                if notice.payload in woken:
                    woken[notice.payload].set()
                else:
                    woken_for_all.set()  # a project this process has not met yet
        finally:
            with contextlib.suppress(Exception):
                await connection.close()
            await relay.pool.putconn(connection)

    woken_for_all = asyncio.Event()
    listening = asyncio.create_task(listen())
    try:
        try:
            await relay.features.start()
        except NotLeader as over:
            _say(f"relay: {over}. This control plane stops.")
            return
        while not stop.is_set() and not replaced.is_set():
            if not await leadership.held():
                _say("relay: the lock on the installation is lost. This control plane stops.")
                return
            # So that a worker can tell this Redis is the one the control plane uses.
            await broker.set(_marker(relay.installation), leadership.term, ex=int(every * 6))
            async with relay.pool.connection() as connection:
                projects = await (
                    await connection.execute("SELECT project_id FROM projects ORDER BY project_id")
                ).fetchall()
            for project in projects:
                project_id = project["project_id"]
                if project_id not in tasks or tasks[project_id].done():
                    woken.setdefault(project_id, asyncio.Event())
                    tasks[project_id] = asyncio.create_task(one_project(project_id))
            with contextlib.suppress(NotLeader):
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
        await relay.invocations.close()
        await leadership.release()


async def transport_is_the_control_planes(relay: Relay, broker: redis.Redis) -> str:
    """Empty if the Redis this process was given is the one the control plane is using;
    otherwise why not. The control plane leaves its term there, and the term is on
    record in PostgreSQL: a marker that is missing, or of an earlier term, is another
    Redis than the one commands are being published to."""
    marked = await broker.get(_marker(relay.installation))
    async with relay.pool.connection() as connection:
        term = await current_term(connection)
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
    checking_transport_every: float = CHECKING_TRANSPORT_EVERY,
) -> None:
    """Serve one role across every project until told to stop."""
    session = await WorkerSession.start(relay.pool, role)
    _say(f"relay: worker {session.worker_id} for {role}")

    async def then(project_id: str) -> None:
        await wake(relay.pool, relay.installation, project_id)

    serving: Any = Worker(
        session, relay.claims, relay.invocations, provider, relay.tasks[role], relay.boundaries,
        runtime, then=then,
    )  # fmt: skip
    loop = asyncio.get_running_loop()
    checked, trouble = float("-inf"), "not checked yet"
    try:
        while not stop.is_set():
            if trouble or loop.time() - checked > checking_transport_every:
                was, trouble = trouble, await transport_is_the_control_planes(relay, broker)
                checked = loop.time()
                if trouble and trouble != was:
                    _say(f"relay: {role} launches nothing: {trouble}")
            did = 0
            if not trouble:
                async with relay.pool.connection() as connection:
                    projects = await (
                        await connection.execute(
                            "SELECT project_id FROM projects ORDER BY project_id"
                        )
                    ).fetchall()
                for project in projects:
                    did += len(await serving.serve_once(relay.streams, project["project_id"]))
            if not did:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), idle)
    finally:
        await session.stop()
