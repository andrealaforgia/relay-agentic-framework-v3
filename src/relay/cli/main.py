"""The command line. Each command opens the installation, does one thing, and exits;
`up` and `worker start` are the two that run for as long as Relay does."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import signal
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as redis

from relay import processes
from relay.application import authority
from relay.application.leadership import AlreadyLed, installation_of
from relay.cli import journal, terminal
from relay.cli.enrolment import DEFAULT_IMAGE, Asked, CannotEnrol, enrol
from relay.persistence.database import Pool, migrate, open_pool
from relay.providers.boundary_suite import runtime_now
from relay.providers.claude import ClaudeAdapter
from relay.runtime import ROLES, NotConfigured, assemble, bootstrap, stated_policy


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
        except (AlreadyLed, processes.CannotListen) as cannot:
            print(f"relay: {cannot}", file=sys.stderr)
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
                try:
                    async with pool.connection() as connection:
                        stated["policy"] = await stated_policy(connection)
                except Exception:  # noqa: BLE001 - nothing is launched on a statement not read
                    stated["policy"] = None
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
                # And where a call is admitted, the statement is read again, in that
                # transaction: a call worked out from one that has since changed is not
                # launched.
                stated=stated_policy,
            )
        except processes.LostIdentity as lost:
            print(f"relay: {lost}", file=sys.stderr)
            return 1
        finally:
            reading.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reading
    return 0


async def _installation(pool: Pool) -> str:
    async with pool.connection() as connection:
        return await installation_of(connection)


async def _init(asked: argparse.Namespace, command: Sequence[str]) -> int:
    async with _opened() as (pool, broker):
        await migrate(pool)
        request = journal.note(journal.journal_file(), command, await _installation(pool), None)
        print(f"request {request.request_id}")
        try:
            said = await enrol(
                pool,
                broker,
                Path.cwd(),
                Asked(
                    project=asked.project,
                    repository=asked.repository,
                    trunk=asked.trunk,
                    workspaces=asked.workspaces,
                    mirrors=asked.mirrors,
                    image=asked.image,
                    definitions=asked.definitions or [],
                ),
                terminal.read_secret,
            )
        except (CannotEnrol, terminal.NoTerminal, NotConfigured) as cannot:
            print(f"relay: {cannot}", file=sys.stderr)
            return 1
    for line in said:
        print(line)
    return 0


async def _passphrase(command: Sequence[str]) -> int:
    async with _opened() as (pool, _):
        request = journal.note(journal.journal_file(), command, await _installation(pool), None)
        print(f"request {request.request_id}")
        try:
            current = terminal.read_secret("The passphrase as it is now: ")
            await authority.require(pool, current)
            new = terminal.read_secret(
                f"The new passphrase (at least {authority.SHORTEST} characters): "
            )
            if new != terminal.read_secret("Again: "):
                print("relay: the two did not match: the passphrase is unchanged", file=sys.stderr)
                return 1
            await authority.change(pool, current, new)
        except (authority.Refused, terminal.NoTerminal) as refused:
            print(f"relay: {refused}", file=sys.stderr)
            return 1
    print("the passphrase: changed")
    return 0


async def _policy(asked: argparse.Namespace, command: Sequence[str]) -> int:
    async with _opened() as (pool, _):
        if asked.doing == "show":
            policy = await authority.policy_of(pool)
            if policy is None:
                print("no statement of policy has been made: workers launch nothing")
                return 0
            print(
                f"revision {policy.revision}, stated by {policy.stated_by} at {policy.stated_at}:"
            )
            print(policy.statement)
            return 0
        request = journal.note(journal.journal_file(), command, await _installation(pool), None)
        print(f"request {request.request_id}")
        try:
            # Shown where the passphrase is then read: what is confirmed is what is seen.
            terminal.say(
                "You are stating, as the operator, what policy this host is given from "
                f"elsewhere:\n\n  {asked.statement.strip()}\n"
            )
            policy = await authority.state_policy(
                pool,
                terminal.read_secret("The Product Owner's passphrase: "),
                asked.statement,
                getpass.getuser(),
            )
        except (authority.Refused, terminal.NoTerminal) as refused:
            print(f"relay: {refused}", file=sys.stderr)
            return 1
    print(f"the statement of policy: revision {policy.revision}, by {policy.stated_by}")
    return 0


def _requests() -> int:
    for request in journal.read(journal.journal_file()):
        project = f" [{request.project}]" if request.project else ""
        print(f"{request.request_id}  {request.at}{project}  relay {' '.join(request.command)}")
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
    init = commands.add_parser(
        "init", help="set the installation up, register this checkout's project, set the passphrase"
    )
    init.add_argument("--project", help="the project's id (default: the checkout's name)")
    init.add_argument("--repository", help="the repository's address (default: the one remote)")
    init.add_argument("--trunk", help="the trunk branch (default: the branch checked out)")
    init.add_argument("--workspaces", help="where runs work (outside the home directory)")
    init.add_argument("--mirrors", help="where Relay keeps its copies of repositories")
    init.add_argument("--image", help=f"the oracle's container image (default {DEFAULT_IMAGE})")
    init.add_argument("--definitions", action="append", help="a directory of agent definitions")
    commands.add_parser("passphrase", help="change the Product Owner's passphrase")
    policy = commands.add_parser("policy", help="the operator's statement of remote policy")
    of_policy = policy.add_subparsers(dest="doing", required=True)
    of_policy.add_parser("state", help="state it, with the passphrase").add_argument("statement")
    of_policy.add_parser("show", help="show the statement in force")
    commands.add_parser("requests", help="the requests made from this machine, to repeat one")
    given = list(sys.argv[1:] if argv is None else argv)
    asked = parser.parse_args(given)
    try:
        if asked.command == "up":
            return asyncio.run(_up(asked.every))
        if asked.command == "init":
            return asyncio.run(_init(asked, given))
        if asked.command == "passphrase":
            return asyncio.run(_passphrase(given))
        if asked.command == "policy":
            return asyncio.run(_policy(asked, given))
        if asked.command == "requests":
            return _requests()
        return asyncio.run(_worker(asked.role))
    except NotConfigured as missing:
        print(f"relay: {missing}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
