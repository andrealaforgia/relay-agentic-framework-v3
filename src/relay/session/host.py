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
from decimal import Decimal, localcontext
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
    # Whether what the run said and the session it named are Relay's to use: it started
    # with what it was given, as the session it was asked to be, and ended as that one.
    accepted: bool = False

    @property
    def could_not_resume(self) -> bool:
        """The provider said, before anything was started or charged, that it has no such
        session. Only this is a reason to make the turn again with a new one: a run that
        merely ended without saying how may have been a call, and is left as one."""
        if self.started is not None or self.terminal is None or self.terminal.succeeded:
            return False
        # Nothing charged and nothing used: the real refusal reports a cost of nought and
        # counters that are all nought, with no turn made.
        if self.terminal.provider_cost_usd not in (None, Decimal(0)):
            return False
        if self.terminal.envelope.get("num_turns") not in (None, 0):
            return False
        if any(
            count
            for each in self.terminal.usage
            for count in (
                each.input_tokens,
                each.output_tokens,
                each.cache_read_tokens,
                each.cache_write_tokens,
            )
        ):
            return False
        said = json.dumps(self.terminal.envelope.get("errors", ""))
        return "No conversation found" in said


class Abandon:
    """A way to end a call from outside it: the host was interrupted."""

    def __init__(self) -> None:
        self.asked, self.group = False, 0

    def now(self) -> None:
        self.asked = True
        if self.group:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.group, signal.SIGKILL)


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
    abandon: Abandon | None = None,
) -> Ran:
    """One call of the CLI: what it said is passed on as it streams, once it has started
    as the session it was asked to be, with what it was given and nothing else."""
    wanted = str(resume if resume is not None else new)
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
    if abandon is not None:
        abandon.group = process.pid
        if abandon.asked:
            abandon.now()
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
                        if isinstance(event, Started) and started is None:
                            started = event
                            ended_for = beyond(event) or (
                                None
                                if event.session_id == wanted
                                else f"the run started as session {event.session_id!r}, "
                                f"and was asked to be {wanted}"
                            )
                        elif isinstance(event, Said) and started is not None and not ended_for:
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
        # Ended by Relay after it was launched: what it did before that nobody can say,
        # and whatever it was charged is not known.
        return Ran("uncertain", ended_for, started)
    if terminal is None:
        # Launched, and gone without saying how it ended: it may have been a call.
        return Ran(
            "uncertain",
            "the host was interrupted and the call ended"
            if abandon is not None and abandon.asked
            else "the run ended without a result",
            started,
        )
    if started is None:
        # A result from a run that never said what it started with. What it reports
        # having used is kept; what it says, and the session it names, are not Relay's
        # to pass on or to continue.
        return Ran(
            "errored",
            terminal.terminal_reason
            or terminal.subtype
            or "a result came from a run that never said what it started with",
            None,
            terminal,
        )
    if terminal.session_id != wanted:
        return Ran(
            "errored",
            f"the result is of session {terminal.session_id!r}, and the call was {wanted}",
            started,
            terminal,
        )
    if terminal.succeeded:
        return Ran("applied", None, started, terminal, accepted=True)
    return Ran(
        "errored", terminal.terminal_reason or terminal.subtype, started, terminal, accepted=True
    )


def from_the_record(
    context: Mapping[str, Any], earlier: Sequence[Mapping[str, Any]], said: str
) -> str:
    """What a new provider session is told with the person's words: the conversation as
    Relay has it, since nothing of an earlier session's transcript comes with it. What
    was said so far in this conversation, both sides, and where things stand."""
    so_far = (
        "What has been said in this conversation so far, as Relay recorded it:\n\n"
        f"{json.dumps(list(earlier), indent=2, default=str)}\n\n"
        if earlier
        else ""
    )
    return (
        "This is a new session of a conversation Relay has on record. What follows is "
        "Relay's record of where things stand, as `context` gives it: the latest analysis, "
        "the notes and the Product Owner's answers to them, what is waiting, and what was "
        "prepared and not yet confirmed.\n\n"
        f"{json.dumps(context, indent=2, default=str)}\n\n"
        f"{so_far}"
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
    """One turn: recorded before its call, made, and settled once. Once the call has
    been made its outcome is recorded whatever becomes of this host: an interrupt ends
    the call, and what is known of it by then is written down before the interrupt is
    obeyed."""
    abandon = Abandon()
    whole = asyncio.ensure_future(
        _turn(pool, launch, config, conversation, project_id, said, heard, abandon)
    )
    try:
        return await asyncio.shield(whole)
    except asyncio.CancelledError:
        abandon.now()
        with contextlib.suppress(Exception):
            await asyncio.shield(whole)
        raise


async def _turn(
    pool: Pool,
    launch: Launch,
    config: Path,
    conversation: uuid.UUID,
    project_id: str,
    said: str,
    heard: Callable[[str], None],
    abandon: Abandon,
) -> Ran:
    made = await conversations.begin_turn(
        pool, conversation, said, launch.model, gives_up_after=launch.timeout + 60
    )
    theirs = await conversations.provider_session_of(pool, conversation)
    continued, new = theirs is not None, uuid.uuid4()
    ran: Ran | None = None
    if theirs is not None:
        ran = await call(launch, config, said, resume=theirs, new=new, heard=heard, abandon=abandon)
        if ran.could_not_resume and not abandon.asked:
            # The provider's session is not there to continue, and it said so before
            # anything was started. The turn is made all the same, with a new one
            # (3C-3.8). Nothing else is a reason to make a second call for one turn.
            continued, ran = False, None
    if ran is None:
        briefing = from_the_record(
            await conversations.context(pool, project_id),
            await conversations.turns_before(pool, conversation, made.number),
            said,
        )
        ran = await call(
            launch, config, briefing, resume=None, new=new, heard=heard, abandon=abandon
        )
    # Whatever a result says was used is kept, whether or not the rest of it is.
    used, charged, estimated = (
        use_of(launch.prices, ran.terminal) if ran.terminal is not None else (None, None, None)
    )
    await conversations.settle_turn(
        pool,
        made.turn_id,
        ran.state,
        reply=ran.terminal.text if ran.terminal is not None and ran.accepted else None,
        reason=ran.reason,
        continued=continued,
        # Continued next time only if it is the session this call was, start to end.
        provider_session=(
            _identity(ran.terminal.session_id)
            if ran.terminal is not None and ran.accepted
            else None
        ),
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


def _amount(amount: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 100
        return amount.quantize(Decimal("0.00000001")).normalize()


def cost_line(ran: Ran, prices: PriceTable) -> str:
    """What a turn cost, as far as it is known (PO-9)."""
    if ran.terminal is None:
        return "this turn's cost is not known"
    _, charged, estimated = use_of(prices, ran.terminal)
    # To a hundred-millionth of a dollar: not with the noise of a binary fraction.
    charged = None if charged is None else _amount(charged)
    estimated = None if estimated is None else _amount(estimated)
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
    await conversations.give_up_on_turns_left_open(pool, project_id)
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


def for_the_server(environ: Mapping[str, str], directory: Path) -> dict[str, str]:
    """What the server needs to find this installation, and nothing it has no use for. A
    configuration file named relative to where the person ran the command is given as
    the file it is: the server is started somewhere else."""
    told = {name: value for name, value in environ.items() if name.startswith("RELAY_")}
    if told.get("RELAY_CONFIG"):
        told["RELAY_CONFIG"] = str((directory / told["RELAY_CONFIG"]).resolve())
    return told


def server_command(project_id: str) -> list[str]:
    """How Relay's server is started for a session of this project: this same Relay, by
    the interpreter that is running it."""
    return [sys.executable, "-m", "relay.cli.main", "session-server", "--project", project_id]
