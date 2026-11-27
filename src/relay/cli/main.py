"""The command line. Each command opens the installation, does one thing, and exits;
`up` and `worker start` are the two that run for as long as Relay does."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import getpass
import json
import signal
import sys
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as redis

from relay import processes
from relay.application import authority
from relay.application.boundaries import Boundaries
from relay.application.boundary_runs import Attempt, BoundaryRuns, RunRefused
from relay.application.leadership import AlreadyLed, installation_of
from relay.cli import doctor, journal, terminal
from relay.cli.enrolment import DEFAULT_IMAGE, Asked, CannotEnrol, enrol
from relay.git.checkout import require_credential_free
from relay.persistence.database import Pool, migrate, open_pool, schema_state
from relay.providers.boundary_suite import SuiteResult, run_suite, runtime_now
from relay.providers.claude import ClaudeAdapter
from relay.runtime import (
    ROLES,
    NotConfigured,
    assemble,
    bootstrap,
    settings_of,
    stated_policy,
)


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
        if asked.repository:
            try:
                # Before anything of this command is written down: an address with a
                # secret in it is refused, and is kept nowhere and shown nowhere.
                require_credential_free(asked.repository)
            except Exception as refused:  # noqa: BLE001 - whatever is wrong with it, said
                print(f"relay: the repository's address cannot be used: {refused}", file=sys.stderr)
                return 1
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


async def _noted(
    pool: Pool, command: Sequence[str], request_id: str | None, action: str
) -> tuple[journal.Request, str | None]:
    """Write the request down, and say what it did if it is one sent again and it did
    something. A handle made of another installation is refused: it is answered there."""
    installation = await _installation(pool)
    request = journal.note(journal.journal_file(), command, installation, None, request_id)
    if request.installation != installation:
        raise authority.Refused(
            f"request {request.request_id} was made of installation {request.installation}, "
            f"and this is {installation}: nothing is done"
        )
    print(f"request {request.request_id}", flush=True)
    done = await authority.receipt_of(pool, request.request_id)
    if done is None:
        return request, None
    if done["action"] != action:
        raise authority.Refused(f"request {request.request_id} was for something else")
    if action == "policy":
        return request, "on record"
    return request, "the passphrase: changed (as on record: this request changed it)"


async def _passphrase(asked: argparse.Namespace, command: Sequence[str]) -> int:
    async with _opened() as (pool, _):
        try:
            request, done = await _noted(pool, command, asked.request_id, "passphrase")
            if done is not None:
                print(done)
                return 0
            current = terminal.read_secret("The passphrase as it is now: ")
            await authority.require(pool, current)
            new = terminal.read_secret(
                f"The new passphrase (at least {authority.SHORTEST} characters): "
            )
            if new != terminal.read_secret("Again: "):
                print("relay: the two did not match: the passphrase is unchanged", file=sys.stderr)
                return 1
            await authority.change(pool, current, new, request.request_id)
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
        try:
            request, done = await _noted(pool, command, asked.request_id, "policy")
            if done is not None:
                # Answered from its receipt, which is of one statement: the same handle
                # with another statement is refused there. No passphrase is asked for,
                # since nothing is decided.
                made = await authority.state_policy(
                    pool, "", asked.statement, getpass.getuser(), request.request_id
                )
                print(
                    f"the statement of policy: revision {made.revision}, by {made.stated_by} "
                    "(as on record: this request made it, and makes no other)"
                )
                return 0
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
                request.request_id,
            )
        except (authority.Refused, terminal.NoTerminal) as refused:
            print(f"relay: {refused}", file=sys.stderr)
            return 1
    print(f"the statement of policy: revision {policy.revision}, by {policy.stated_by}")
    return 0


async def _doctor(asked: argparse.Namespace, command: Sequence[str]) -> int:
    async with _opened() as (pool, broker):
        if not asked.boundary:
            # Reads, and changes nothing: no migration is applied to find out what is wrong.
            found = await doctor.examine(pool, broker)
            if asked.json:
                print(json.dumps([dataclasses.asdict(each) for each in found], indent=2))
            else:
                print(doctor.report(found))
            return 0 if all(each.ok for each in found) else 1
        try:
            pending, drifted = await asyncio.wait_for(schema_state(pool), doctor.DATABASE_WITHIN)
        except Exception as unreachable:  # noqa: BLE001 - whatever it is, it is said
            print(f"relay: the database cannot be reached: {unreachable!r}", file=sys.stderr)
            return 1
        if pending or drifted:
            print(
                "relay: the database is not set up for this build: see `relay doctor`",
                file=sys.stderr,
            )
            return 1
        installation = await _installation(pool)
        settings, policy = await settings_of(pool), await authority.policy_of(pool)
        if policy is None or any(not settings.get(name) for name in doctor.NEEDED):
            print(
                "relay: the bounds cannot be tried until the installation is set up and the "
                "policy stated: see `relay doctor`",
                file=sys.stderr,
            )
            return 1
        binary = str(settings.get("cli", "claude"))
        runtime = runtime_now(policy.statement, binary)
        request = journal.note(
            journal.journal_file(), command, installation, None, asked.request_id
        )
        if request.installation != installation:
            # The handle is of a request made of another installation. It is answered
            # from that one, and buys nothing from this.
            print(
                f"relay: request {request.request_id} was made of installation "
                f"{request.installation}, and this is {installation}: nothing is run",
                file=sys.stderr,
            )
            return 1
        print(f"request {request.request_id}", flush=True)

        def said_first(roles: list[str]) -> None:
            # Before anything is launched, whatever asked for it: a first request, one
            # sent again that had not got as far as launching, or a retry.
            print(
                f"{len(roles)} paid run(s) of {doctor.SUITE_MODEL}, for the "
                f"{', '.join(roles)}: at most {doctor.A_RUN_COSTS_AT_MOST} US dollars each",
                flush=True,
            )

        async def suite(role: str, on: Mapping[str, str]) -> SuiteResult:
            return await run_suite(
                role,
                Path(settings["workspaces"]),
                binary=binary,
                model=doctor.SUITE_MODEL,
                withheld=[Path(settings["mirrors"])] if role == "builder" else [],
                remote_policy=policy.statement,
            )

        made = await doctor.run_boundaries(
            pool,
            BoundaryRuns(pool, Boundaries(pool), suite),
            settings,
            runtime,
            request.request_id,
            installation,
            uuid.UUID(asked.retry) if asked.retry else None,
            said_first,
        )
    if not made:
        print("every role's bounds had lately been seen to hold: nothing was run")
    for each in made:
        if isinstance(each, RunRefused):
            print(f"NOT  {each}")
        else:
            how = "run now" if each.launched_now else "as on record"
            detail = f": {each.detail}" if each.detail else ""
            print(
                f"{'ok ' if each.state in ('passed', 'fresh') else 'NOT'}  the bounds of a "
                f"{each.role}: {each.state}{detail} (attempt {each.attempt_id}, {how})"
            )
    return (
        0
        if all(isinstance(each, Attempt) and each.state in ("passed", "fresh") for each in made)
        else 1
    )


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
    repeat = "repeat a request: what it did is returned, and nothing more is done"
    commands.add_parser("passphrase", help="change the Product Owner's passphrase").add_argument(
        "--request-id", help=repeat
    )
    policy = commands.add_parser("policy", help="the operator's statement of remote policy")
    of_policy = policy.add_subparsers(dest="doing", required=True)
    stating = of_policy.add_parser("state", help="state it, with the passphrase")
    stating.add_argument("statement")
    stating.add_argument("--request-id", help=repeat)
    of_policy.add_parser("show", help="show the statement in force")
    commands.add_parser("requests", help="the requests made from this machine, to repeat one")
    doctoring = commands.add_parser(
        "doctor", help="what would stop Relay working here, and what to do about each"
    )
    doctoring.add_argument("--json", action="store_true", help="the same facts, as JSON")
    doctoring.add_argument(
        "--boundary",
        action="store_true",
        help="run the boundary suite where no fresh pass is on record (paid calls)",
    )
    doctoring.add_argument(
        "--retry", metavar="ATTEMPT", help="buy another run for an uncertain one"
    )
    doctoring.add_argument(
        "--request-id", help="repeat a request: what it did is returned, nothing is run again"
    )
    given = list(sys.argv[1:] if argv is None else argv)
    asked = parser.parse_args(given)
    try:
        if asked.command == "up":
            return asyncio.run(_up(asked.every))
        if asked.command == "init":
            return asyncio.run(_init(asked, given))
        if asked.command == "passphrase":
            return asyncio.run(_passphrase(asked, given))
        if asked.command == "policy":
            return asyncio.run(_policy(asked, given))
        if asked.command == "requests":
            return _requests()
        if asked.command == "doctor":
            return asyncio.run(_doctor(asked, given))
        return asyncio.run(_worker(asked.role))
    except NotConfigured as missing:
        print(f"relay: {missing}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
