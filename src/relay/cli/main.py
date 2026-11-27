"""The command line. Each command opens the installation, does one thing, and exits;
`up` and `worker start` are the two that run for as long as Relay does."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import redis.asyncio as redis

from relay import processes
from relay.application.leadership import AlreadyLed
from relay.persistence.database import Pool, migrate, open_pool
from relay.providers.boundary_suite import runtime_now
from relay.providers.claude import ClaudeAdapter
from relay.runtime import ROLES, NotConfigured, assemble, bootstrap


@asynccontextmanager
async def _opened() -> AsyncIterator[tuple[Pool, redis.Redis]]:
    told = bootstrap()
    broker = redis.from_url(told.redis, decode_responses=True)
    try:
        async with open_pool(told.database, schema=told.schema) as pool:
            yield pool, broker
    finally:
        await broker.aclose()


def _until_told_to_stop() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for told in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(told, stop.set)
    return stop


async def _up(every: float) -> int:
    async with _opened() as (pool, broker):
        await migrate(pool)
        relay = await assemble(pool, broker, control_plane=True)
        try:
            await processes.control_plane(relay, broker, _until_told_to_stop(), every=every)
        except AlreadyLed as led:
            print(f"relay: {led}", file=sys.stderr)
            return 1
    return 0


async def _worker(role: str) -> int:
    async with _opened() as (pool, broker):
        relay = await assemble(pool, broker)
        binary = str(relay.settings.get("cli", "claude"))
        stated: dict[str, str | None] = {"policy": None}

        async def reading_the_statement() -> None:
            """What the operator has stated of this host's remote policy (D-10), as the
            installation has it now: read again and again, since it can be stated or
            restated while this worker runs."""
            while True:
                async with pool.connection() as connection:
                    row = await (
                        await connection.execute(
                            "SELECT policy->>'statement' AS statement FROM installation"
                        )
                    ).fetchone()
                stated["policy"] = row["statement"] if row else None
                await asyncio.sleep(1.0)

        reading = asyncio.create_task(reading_the_statement())
        try:
            await processes.worker(
                relay,
                broker,
                role,
                ClaudeAdapter(binary),
                lambda: runtime_now(stated["policy"], binary),
                _until_told_to_stop(),
            )
        finally:
            reading.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reading
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relay")
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("up", help="run the control plane")
    up.add_argument("--every", type=float, default=processes.EVERY, help=argparse.SUPPRESS)
    worker = commands.add_parser("worker", help="run a worker")
    of_worker = worker.add_subparsers(dest="doing", required=True)
    start = of_worker.add_parser("start", help="serve one role")
    start.add_argument("--role", required=True, choices=ROLES)
    asked = parser.parse_args(argv)
    try:
        if asked.command == "up":
            return asyncio.run(_up(asked.every))
        return asyncio.run(_worker(asked.role))
    except NotConfigured as missing:
        print(f"relay: {missing}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
