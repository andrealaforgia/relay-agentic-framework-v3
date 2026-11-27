"""The command line. Each command opens the installation, does one thing, and exits;
`up` and `worker start` are the two that run for as long as Relay does."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import getpass
import json
import os
import signal
import sys
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as redis

from relay import processes
from relay.application import authority, prepared
from relay.application.boundaries import Boundaries
from relay.application.boundary_runs import Attempt, BoundaryRuns, RunRefused
from relay.application.inbox import Inbox
from relay.application.integration import IntegrationRefused
from relay.application.leadership import AlreadyLed, installation_of
from relay.cli import deciding, doctor, journal, reading, terminal
from relay.cli.enrolment import DEFAULT_IMAGE, Asked, CannotEnrol, enrol
from relay.cli.selection import NoProject, select_project
from relay.domain.story import ActionRefused
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
    wake,
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


async def _handle(
    pool: Pool,
    command: Sequence[str],
    given: str | None,
    action: str,
    asked: str,
    project: str | None = None,
) -> journal.Request:
    """The handle this command acts under: the one it was given, or a new one. It is
    taken in the database first, for this action asking this thing: a handle that is
    another request's is refused there, before anything is written on this machine, so a
    mistaken use of it leaves no local entry to stand in the way of its proper one. Only
    then is it written in the journal, where a handle of another installation, or of
    another command, is refused in its turn."""
    installation = await _installation(pool)
    request_id = given or str(uuid.uuid7())
    await authority.take_handle(pool, request_id, action, asked)
    request = journal.note(journal.journal_file(), command, installation, project, request_id)
    if request.installation != installation:
        raise authority.Refused(
            f"request {request.request_id} was made of installation {request.installation}, "
            f"and this is {installation}: nothing is done"
        )
    if (request.command[0] if request.command else "") != action.split()[0]:
        raise authority.Refused(
            f"request {request.request_id} was `relay {' '.join(request.command[:2])}`: a "
            "request's identity is used again only to ask for the same thing. Nothing is done"
        )
    return request


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
        # What is written down is what was understood and checked, option by option:
        # not the words as typed, in which an option given twice is there twice, with
        # whatever the one that was not used held.
        understood = ["init"]
        for option in ("project", "repository", "trunk", "workspaces", "mirrors", "image"):
            if getattr(asked, option):
                understood += [f"--{option}", str(getattr(asked, option))]
        for directory in asked.definitions or []:
            understood += ["--definitions", directory]
        request = journal.note(journal.journal_file(), understood, await _installation(pool), None)
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
    pool: Pool, command: Sequence[str], request_id: str | None, action: str, asked: str = ""
) -> tuple[journal.Request, str | None]:
    """Take the handle and write the request down, and say what it did if it is one sent
    again and it did something."""
    request = await _handle(pool, command, request_id, action, asked)
    print(f"request {request.request_id}", flush=True)
    done = await authority.receipt_of(pool, request.request_id)
    if done is None:
        return request, None
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
            if policy.stated_at is None:
                print(
                    f"revision {policy.revision}, stated before who made it and when were "
                    "kept (restate it with `relay policy state` to put that on record):"
                )
            else:
                print(
                    f"revision {policy.revision}, stated by {policy.stated_by} "
                    f"at {policy.stated_at}:"
                )
            print(policy.statement)
            return 0
        try:
            request, done = await _noted(
                pool, command, asked.request_id, "policy", asked.statement.strip()
            )
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
    try:
        bootstrap()
    except NotConfigured as missing:
        # The first thing that can be wrong on a first use, and what doctor is for: said
        # as a finding, in the form asked for, with what to do.
        found = [
            doctor.Finding(
                "where the database and Redis are",
                False,
                str(missing),
                "set RELAY_DATABASE_URL and RELAY_REDIS_URL, or write them under [relay] in "
                "the file RELAY_CONFIG names or in ~/.config/relay/relay.toml",
            )
        ]
        print(
            json.dumps([dataclasses.asdict(each) for each in found], indent=2)
            if asked.json
            else doctor.report(found)
        )
        return 1
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
        try:
            # The handle may be another installation's, or a request for something else.
            # It is answered where and as it was made, and buys nothing here.
            request = await _handle(
                pool,
                command,
                asked.request_id,
                "doctor",
                f"boundary runs, retrying {asked.retry or 'none'}",
            )
        except authority.Refused as refused:
            print(f"relay: {refused}", file=sys.stderr)
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

        async def suite(role: str, on: Mapping[str, str], expecting: str) -> SuiteResult:
            return await run_suite(
                role,
                Path(settings["workspaces"]),
                binary=binary,
                model=doctor.SUITE_MODEL,
                withheld=[Path(settings["mirrors"])] if role == "builder" else [],
                remote_policy=policy.statement,
                # The suite reads the runtime for itself before each launch, and holds
                # it to this: a CLI updated between two roles launches no second run.
                expecting=expecting,
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


async def _project(pool: Pool, asked: argparse.Namespace) -> str:
    """The project this command means. A request sent again means the project it was
    resolved to when it was first made, wherever it is sent again from."""
    if getattr(asked, "request_id", None):
        for known in journal.read(journal.journal_file()):
            if known.request_id == asked.request_id and known.project:
                return known.project
    return await select_project(pool, asked.project, os.environ, Path.cwd())


async def _decide(asked: argparse.Namespace, command: Sequence[str]) -> int:
    """One decision of the Product Owner's: resolved against the record, shown on the
    terminal, confirmed by the passphrase, carried out under a handle taken first, and
    answered with the service's receipt."""
    verb = asked.verb
    async with _opened() as (pool, broker):
        try:
            project = await _project(pool, asked)
            if asked.request_id and (
                done := await deciding.receipt_on_record(pool, asked.request_id)
            ):
                # Sent again: it decided what it decided, and decides nothing now. No
                # passphrase is asked for, and what it was about need no longer be there.
                print(f"request {asked.request_id}")
                print(f"{done.disposition}: {done.outcome} (as on record)")
                return 0
            relay = await assemble(pool, broker)
            decision = await deciding.resolve(
                relay,
                project,
                verb,
                getattr(asked, "target", "") or "",
                getattr(asked, "text", None),
                getattr(asked, "reason", None),
            )
            request = await _handle(
                pool, command, asked.request_id, command[0], decision.asked, project
            )
            print(f"request {request.request_id}", flush=True)
            # Shown where the passphrase is then read: what is confirmed is what is seen.
            terminal.say(decision.shown)
            await authority.require(pool, terminal.read_secret("The Product Owner's passphrase: "))
            receipt = await decision.carry_out(request.request_id)
            # Whatever the decision leaves for Relay to do by itself is the control
            # plane's: it is told, here, after the decision has committed.
            await wake(pool, relay.installation, project)
        except (
            deciding.CannotDecide,
            NoProject,
            authority.Refused,
            terminal.NoTerminal,
            ActionRefused,
            IntegrationRefused,
            NotConfigured,
            LookupError,
        ) as refused:
            print(f"relay: {refused}", file=sys.stderr)
            return 1
    print(f"{receipt.disposition}: {receipt.outcome}")
    return 0 if receipt.disposition == "effective" else 1


async def _prepare(asked: argparse.Namespace) -> int:
    """Record an action for the Product Owner to confirm. It decides nothing, asks for no
    passphrase and needs no terminal: this is what a model in the session may do."""
    async with _opened() as (pool, broker):
        try:
            project = await select_project(pool, asked.project, os.environ, Path.cwd())
            relay = await assemble(pool, broker)
            # Resolved now, against the record as it stands: what cannot be done is
            # refused here, and what can is kept with the record as it was shown.
            decision = await deciding.resolve(
                relay, project, asked.verb, asked.target, asked.text, asked.reason
            )
            made = await prepared.prepare(
                pool, project, asked.verb, asked.target, asked.text, asked.reason,
                asked.why, decision.shown, decision.basis,
            )  # fmt: skip
        except (deciding.CannotDecide, NoProject, NotConfigured, LookupError, ValueError) as no:
            print(f"relay: {no}", file=sys.stderr)
            return 1
    print(f"prepared {made.code}")
    print(
        f"Nothing has been decided. The Product Owner confirms it at a terminal of their "
        f"own, within the hour: `relay confirm {made.code}`"
    )
    return 0


async def _confirm(asked: argparse.Namespace, command: Sequence[str]) -> int:
    """The Product Owner takes a prepared action: shown as it was prepared, held to the
    record as it was then, confirmed by the passphrase. The code is the handle: confirmed,
    it returns its receipt for good; stale or expired, it does nothing, and says so."""
    code = asked.code.strip().lower()
    request_id = f"confirm:{code}"
    async with _opened() as (pool, broker):
        try:
            made = await prepared.find(pool, code)
            if made is None:
                raise deciding.CannotDecide(f"nothing was prepared under the code {code!r}")
            if done := await deciding.receipt_on_record(pool, request_id):
                print(f"{done.disposition}: {done.outcome} (as on record: this code was confirmed)")
                return 0
            if made.ended is not None:
                raise deciding.CannotDecide(f"{made.ended}. Nothing was done under this code")
            project = await select_project(
                pool, asked.project or made.project_id, os.environ, Path.cwd()
            )
            if project != made.project_id:
                raise deciding.CannotDecide(
                    f"this was prepared for {made.project_id}, and the project meant here is "
                    f"{project}: nothing is done"
                )
            if made.expired:
                await prepared.end(pool, code, "this expired unused, an hour after it was prepared")
                raise deciding.CannotDecide(
                    "this expired unused, an hour after it was prepared. Nothing was done"
                )
            relay = await assemble(pool, broker)
            try:
                # Resolved against the record as it stands, and compared with the record
                # as it was prepared against: if it has moved, the code is stale. What is
                # then carried out is held, where the service decides it, to the record
                # as it has just been shown.
                decision = await deciding.resolve(
                    relay, project, made.verb, made.target, made.said, made.reason
                )
                moved = decision.basis != made.basis
            except deciding.CannotDecide:
                moved = True
            if moved:
                # What it was prepared against is closed, or is no longer what was
                # shown. That is this code's answer, for good.
                await prepared.end(
                    pool, code, "stale: what this was prepared against has changed since"
                )
                print(
                    "stale: nothing changed. What this was prepared against has been "
                    "decided, or has changed, since it was prepared"
                )
                return 1
            await _handle(pool, command, request_id, "confirm", code, project)
            print(f"request {request_id}", flush=True)
            terminal.say(
                f"Prepared for you to confirm, and nothing of it has been done:\n\n"
                f"{decision.shown}\nWhy it is proposed: {made.why}\n"
            )
            await authority.require(pool, terminal.read_secret("The Product Owner's passphrase: "))
            receipt = await decision.carry_out(request_id)
            await wake(pool, relay.installation, project)
        except (
            deciding.CannotDecide,
            NoProject,
            authority.Refused,
            terminal.NoTerminal,
            ActionRefused,
            IntegrationRefused,
            NotConfigured,
            LookupError,
        ) as refused:
            print(f"relay: {refused}", file=sys.stderr)
            return 1
    print(f"{receipt.disposition}: {receipt.outcome}")
    return 0 if receipt.disposition == "effective" else 1


async def _read(asked: argparse.Namespace) -> int:
    """What may be read by anyone: no terminal, no passphrase, and nothing changed."""
    async with _opened() as (pool, _):
        try:
            project = await select_project(pool, asked.project, os.environ, Path.cwd())
            facts: object
            if asked.command == "status":
                facts = await reading.status(pool, project)
                said = reading.status_text(facts)
            elif asked.command == "inbox":
                items = await Inbox(pool).items(project)
                facts, said = (
                    [reading.item_facts(each) for each in items],
                    reading.inbox_text(items),
                )
            elif asked.command == "show":
                item = await Inbox(pool).item(project, asked.target)
                if item is None:
                    raise LookupError(
                        f"nothing waiting in {project} is {asked.target!r}: see `relay inbox`"
                    )
                facts, said = reading.item_facts(item), reading.item_text(item)
            elif asked.command == "roadmap":
                facts = await reading.roadmap(pool, project, asked.feature)
                said = reading.roadmap_text(facts)
            else:
                facts = await reading.ledger(pool, project, asked.subject, asked.since)
                said = reading.ledger_text(facts)
        except (NoProject, LookupError) as refused:
            print(f"relay: {refused}", file=sys.stderr)
            return 1
    print(json.dumps(facts, indent=2, default=str) if asked.json else said)
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
    which = "the project meant (default: RELAY_PROJECT, or this checkout's)"
    # What anyone may read.
    for name, about in (
        ("status", "where each feature, story and behaviour stands, and what is owed"),
        ("inbox", "everything waiting for the Product Owner"),
        ("show", "one item of the inbox in full, as it will be decided on"),
        ("roadmap", "a feature's roadmap, and whether a proposal is waiting"),
        ("ledger", "the events of the project, in order"),
    ):
        looking = commands.add_parser(name, help=about)
        looking.add_argument("--project", help=which)
        looking.add_argument("--json", action="store_true", help="the same facts, as JSON")
        if name == "show":
            looking.add_argument("target", metavar="ITEM")
        if name == "roadmap":
            looking.add_argument("feature", nargs="?")
        if name == "ledger":
            looking.add_argument("--subject", help="only the events about this")
            looking.add_argument("--since", type=int, default=0, help="only events after this one")
    # What only the Product Owner may do: each asks for the passphrase at the terminal.
    for verb, about, takes in (
        ("approve", "approve what is put to you at a gate", ("ITEM",)),
        ("request-changes", "send a proposal back, saying why", ("ITEM", "--reason")),
        ("reject", "reject a story, at its gate or at work, saying why", ("ITEM", "--reason")),
        (
            "retry",
            "allow another try: a call, a refused push, a final check",
            ("ITEM", "--reason?"),
        ),
        ("set-aside", "leave work stopped and take its question out of the inbox", ("ITEM",)),
        ("answer", "answer a question a role asked", ("ITEM", "TEXT")),
        ("replace-decision", "replace a decision in force with another", ("ITEM", "TEXT")),
        ("pause", "pause a feature, saying why", ("ITEM", "--reason")),
        ("resume", "resume a paused feature", ("ITEM",)),
        ("cancel", "cancel a feature for good, saying why", ("ITEM", "--reason")),
    ):
        acting = commands.add_parser(verb, help=about)
        acting.set_defaults(verb=verb)
        acting.add_argument("--project", help=which)
        acting.add_argument("--request-id", help=repeat)
        if "ITEM" in takes:
            acting.add_argument("target", metavar="ITEM")
        if "TEXT" in takes:
            acting.add_argument("text", metavar="TEXT")
        if "--reason" in takes or "--reason?" in takes:
            acting.add_argument("--reason", required="--reason" in takes)
    preparing = commands.add_parser(
        "prepare", help="propose an action for the Product Owner to confirm (decides nothing)"
    )
    preparing.add_argument("verb", choices=[each for each in deciding.VERBS if each != "start"])
    preparing.add_argument("target", metavar="ITEM")
    preparing.add_argument("text", metavar="TEXT", nargs="?")
    preparing.add_argument("--reason", help="the reason the action itself carries")
    preparing.add_argument("--why", required=True, help="why this is proposed")
    preparing.add_argument("--project", help=which)
    confirming = commands.add_parser("confirm", help="take a prepared action, with the passphrase")
    confirming.add_argument("code")
    confirming.add_argument("--project", help=which)
    feature = commands.add_parser("feature", help="a feature")
    of_feature = feature.add_subparsers(dest="doing", required=True)
    starting = of_feature.add_parser("start", help="start a feature from a stated problem")
    starting.set_defaults(verb="start")
    starting.add_argument("text", metavar="PROBLEM")
    starting.add_argument("--project", help=which)
    starting.add_argument("--request-id", help=repeat)
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
        if asked.command in ("status", "inbox", "show", "roadmap", "ledger"):
            return asyncio.run(_read(asked))
        if asked.command == "prepare":
            return asyncio.run(_prepare(asked))
        if asked.command == "confirm":
            return asyncio.run(_confirm(asked, given))
        if getattr(asked, "verb", None):
            return asyncio.run(_decide(asked, given))
        return asyncio.run(_worker(asked.role))
    except NotConfigured as missing:
        print(f"relay: {missing}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
