"""Relay hosts the Product Owner's conversation (3C-2.8).

`relay session` is Relay's own loop at the person's terminal. It reads a line from the
person, records it, and makes one call of the CLI in print mode with the stream output,
as a worker's call is made, continuing the same provider session by its identity. Each
call is given no built-in tool and one server, Relay's own, and its start event is held
to exactly that: a run that starts with anything else is ended.

Continuing the provider's session is an economy, not the mechanism (3C-3.8). A turn whose
provider session cannot be continued is made with a new one, the same tools, and the
conversation's context from Relay's record.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from relay.application import conversations
from relay.application.invocations import use_of
from relay.persistence.database import Pool
from relay.providers.pricing import PRICES, PriceTable
from relay.providers.stream import Said, Started, Terminal, read_line
from relay.runtime import stated_policy
from relay.session.server import NAME, TOOLS

GIVEN = tuple(f"mcp__{NAME}__{each.name}" for each in TOOLS)
_ONE_LINE_LIMIT = 16 * 1024 * 1024

BRIEF = f"""You are the Interpreter in Relay: the Product Owner's way of talking to it.

Relay delivers software for the Product Owner with agents: an Analyst plans a feature as
stories, a Verifier specifies each behaviour as an acceptance test, a Builder makes it
pass, QA and a Sentinel review it, and Relay itself runs the tests and integrates the
work. The Product Owner decides: they approve a roadmap and each story, answer questions,
and say what to do when something stops.

You have the tools of the `{NAME}` server and no others. With them you can read
everything Relay has on record for this project. Start by calling `context`.

You decide nothing, and you cannot. To propose an action, call `prepare` with why you
propose it: it gives a code, and the Product Owner confirms it themselves, at a terminal
of their own, with `relay confirm <code>` and their passphrase. Tell them the code and
plainly what it would do. Never ask for the passphrase, and never suggest they give it to
you: it is what makes a decision theirs.

Keep what you work out where it will outlast this conversation. Record your analysis of
their problem with `record_analysis` whenever it changes. Keep a question you mean to
ask them with `note_question`, and when a message of theirs answers one, call
`note_answered` with that note: Relay records their own words against it.

Say what Relay's record says. Where it says something is unknown, say it is unknown. A
cost Relay worked out itself is an estimate: call it one.
"""


class NotLaunched(Exception):
    """The session is not started, and says why."""


@dataclass(frozen=True)
class Launch:
    """How a turn's call is made: the same for every turn of a session."""

    binary: str
    model: str
    budget_usd: Decimal
    timeout: float
    # The command that starts Relay's server for this conversation, and what it is given.
    server: Sequence[str]
    server_environment: Mapping[str, str]
    workdir: Path
    environment: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    prices: PriceTable = PRICES


@dataclass(frozen=True)
class Ran:
    """How one call of the CLI went."""

    state: str
    reason: str | None = None
    started: Started | None = None
    terminal: Terminal | None = None

    @property
    def never_began(self) -> bool:
        return self.started is None


def servers(launch: Launch) -> dict[str, Any]:
    """The one server a call is given, as the CLI's configuration of servers."""
    return {
        "mcpServers": {
            NAME: {
                "command": launch.server[0],
                "args": list(launch.server[1:]),
                "env": dict(launch.server_environment),
            }
        }
    }


def command(launch: Launch, config: Path, *, resume: uuid.UUID | None, new: uuid.UUID) -> list[str]:
    return [
        launch.binary,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--model", launch.model,
        "--system-prompt", BRIEF,
        # No built-in tool at all, and one server, Relay's own: nothing is taken from
        # the user's, the project's or the working directory's settings, servers,
        # skills or commands.
        "--tools", "",
        "--mcp-config", str(config),
        "--strict-mcp-config",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--allowedTools", ",".join(GIVEN),
        "--max-budget-usd", str(launch.budget_usd),
        *(["--resume", str(resume)] if resume is not None else ["--session-id", str(new)]),
    ]  # fmt: skip


def beyond(started: Started) -> str | None:
    """What a run started with that it was not given, or without that it was: None if its
    inventory is exactly Relay's server and that server's tools."""
    if started.tools is None or started.mcp_servers is None:
        return "the run did not say what tools and servers it started with"
    if set(started.mcp_servers) != {NAME}:
        return f"the run started with servers {sorted(started.mcp_servers)}, not {NAME} alone"
    extra, missing = set(started.tools) - set(GIVEN), set(GIVEN) - set(started.tools)
    if extra:
        return f"the run started with tools it was not given: {', '.join(sorted(extra))}"
    if missing:
        return f"the run started without Relay's tools: {', '.join(sorted(missing))}"
    return None


async def call(
    launch: Launch,
    config: Path,
    briefing: str,
    *,
    resume: uuid.UUID | None,
    new: uuid.UUID,
    heard: Callable[[str], None],
) -> Ran:
    """One call of the CLI: what it said is passed on as it streams."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command(launch, config, resume=resume, new=new),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=launch.workdir,
            env=dict(launch.environment),
            start_new_session=True,  # a group of its own: whatever it starts ends with it
            limit=_ONE_LINE_LIMIT,
        )
    except OSError as failure:
        return Ran("not_started", f"could not be launched: {failure}")
    started: Started | None = None
    terminal: Terminal | None = None
    ended_for: str | None = None
    try:
        try:
            async with asyncio.timeout(launch.timeout):
                assert process.stdin is not None and process.stdout is not None
                process.stdin.write(briefing.encode())
                with contextlib.suppress(ConnectionError):
                    await process.stdin.drain()
                process.stdin.close()
                async for raw in process.stdout:
                    for event in read_line(raw.decode(errors="replace")):
                        if isinstance(event, Started):
                            started = event
                            ended_for = beyond(event)
                        elif isinstance(event, Said) and started is not None:
                            heard(event.text)
                        elif isinstance(event, Terminal):
                            terminal = event
                    if ended_for is not None or terminal is not None:
                        break
        except TimeoutError:
            ended_for = f"the call ran longer than {launch.timeout:g}s and was ended"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await process.wait()
    if ended_for is not None:
        # Ended by Relay after it had started: what it did before that nobody can say,
        # and whatever it was charged is not known.
        return Ran("uncertain" if started is not None else "errored", ended_for, started)
    if terminal is None:
        return Ran(
            "uncertain" if started is not None else "errored",
            "the run ended without a result",
            started,
        )
    if terminal.succeeded:
        return Ran("applied", None, started, terminal)
    return Ran("errored", terminal.terminal_reason or terminal.subtype, started, terminal)


def from_the_record(context: Mapping[str, Any], said: str) -> str:
    """What a new provider session is told with the person's words: the conversation as
    Relay has it, since nothing of an earlier session's transcript comes with it."""
    return (
        "This is a new session of a conversation Relay has on record. What follows is "
        "Relay's record of where things stand, as `context` gives it: the latest analysis, "
        "the notes and the Product Owner's answers to them, what is waiting, and what was "
        "prepared and not yet confirmed.\n\n"
        f"{json.dumps(context, indent=2, default=str)}\n\n"
        "The Product Owner now says:\n\n"
        f"{said}"
    )


async def turn(
    pool: Pool,
    launch: Launch,
    config: Path,
    conversation: uuid.UUID,
    project_id: str,
    said: str,
    heard: Callable[[str], None],
) -> Ran:
    """One turn: recorded before its call, made, and settled once."""
    made = await conversations.begin_turn(pool, conversation, said, launch.model)
    theirs = await conversations.provider_session_of(pool, conversation)
    continued, new = theirs is not None, uuid.uuid4()
    ran: Ran | None = None
    if theirs is not None:
        ran = await call(launch, config, said, resume=theirs, new=new, heard=heard)
        if ran.never_began and ran.state != "not_started":
            # The provider's session could not be continued: it is gone, or was never
            # kept. The turn is made all the same, with a new one (3C-3.8).
            continued, ran = False, None
    if ran is None:
        briefing = from_the_record(await conversations.context(pool, project_id), said)
        ran = await call(launch, config, briefing, resume=None, new=new, heard=heard)
    used, charged, estimated = (
        use_of(launch.prices, ran.terminal) if ran.terminal is not None else (None, None, None)
    )
    session = ran.terminal.session_id if ran.terminal is not None else None
    await conversations.settle_turn(
        pool,
        made.turn_id,
        ran.state,
        reply=ran.terminal.text if ran.terminal is not None else None,
        reason=ran.reason,
        continued=continued,
        provider_session=_identity(session),
        usage=used,
        provider_cost_usd=charged,
        estimated_cost_usd=estimated,
        price_table=launch.prices.version if estimated is not None else None,
        envelope=ran.terminal.envelope if ran.terminal is not None else None,
    )
    return ran


def _identity(text: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(text) if text else None
    except ValueError:
        return None


def cost_line(ran: Ran, prices: PriceTable) -> str:
    """What a turn cost, as far as it is known (PO-9)."""
    if ran.terminal is None:
        return "this turn's cost is not known"
    _, charged, estimated = use_of(prices, ran.terminal)
    return (
        "this turn: "
        + (f"{charged} USD charged by the provider" if charged is not None else "charge unknown")
        + "; "
        + (
            f"{estimated} USD estimated by Relay (an estimate, price table {prices.version})"
            if estimated is not None
            else "no estimate"
        )
    )


async def converse(
    pool: Pool,
    project_id: str,
    launch: Launch,
    *,
    listen: Callable[[], str | None],
    say: Callable[[str], None],
) -> int:
    """The conversation, until the person ends it. Returns how many turns were made."""
    async with pool.connection() as connection:
        if await stated_policy(connection) is None:
            # D-10: with no statement of this host's remote policy, nothing is launched.
            raise NotLaunched(
                "no statement of this host's remote policy has been made, and without one "
                "no call is launched: state it with `relay policy state`"
            )
    await conversations.give_up_on_turns_left_open(pool, project_id, launch.timeout + 60)
    conversation = await conversations.open_conversation(pool, project_id)
    turns = 0
    launch.workdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="relay-session-") as scratch:
        config = Path(scratch) / "servers.json"
        config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        NAME: {
                            **servers(launch)["mcpServers"][NAME],
                            "args": [*launch.server[1:], "--conversation", str(conversation)],
                        }
                    }
                }
            )
        )
        try:
            while True:
                said = await asyncio.to_thread(listen)
                if said is None or said.strip() in ("/quit", "/exit"):
                    break
                if not said.strip():
                    continue
                ran = await turn(pool, launch, config, conversation, project_id, said, say)
                turns += 1
                if ran.state != "applied":
                    say(f"[relay: this turn did not complete: {ran.reason}]")
                say(f"[relay: {cost_line(ran, launch.prices)}]")
        finally:
            await conversations.close_conversation(pool, conversation)
    return turns


def server_command(project_id: str) -> list[str]:
    """How Relay's server is started for a session of this project: this same Relay, by
    the interpreter that is running it."""
    return [sys.executable, "-m", "relay.cli.main", "session-server", "--project", project_id]
