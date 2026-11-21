"""Calling Claude Code headless, once, and saying truthfully how the call ended.

There are four endings, and the difference between them is whether money may have been
spent and whether the result is known:

- returned: a terminal event that says success. Known, paid for.
- errored: a terminal event that says error. Known, paid for, and not a reply.
- not started: the process could not be launched. Nothing was spent.
- uncertain: it was launched and no terminal event was read. It may have completed.

The briefing goes in on standard input, the run gets an environment chosen for it and a
process group of its own, and what it was actually given (its tools, its servers) is
checked against what was asked for as soon as it says.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from relay.providers.definitions import AgentDefinition
from relay.providers.stream import Event, Started, Terminal, read_line

# The tool the CLI adds for itself when a reply has to satisfy a schema.
_STRUCTURED_OUTPUT = "StructuredOutput"
_ALWAYS_PASSED = (
    "--print",
    "--output-format",
    "--verbose",
    "--agents",
    "--agent",
    "--model",
    "--json-schema",
    "--session-id",
    "--tools",
    "--settings",
    "--setting-sources",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--permission-prompts",
    "--no-session-persistence",
)
# What the supervisor holds and a model's process has no business with.
_WITHHELD = re.compile(
    r"RELAY_.*|DATABASE_URL|REDIS_URL|PG[A-Z]+|GIT_.*|GH_.*|GITHUB_.*|SSH_.*", re.IGNORECASE
)
_ONE_LINE_LIMIT = 64 * 1024 * 1024


class ProviderUnavailable(Exception):
    """The installed CLI cannot do what Relay would ask of it. Nothing was called."""


class Outcome(StrEnum):
    RETURNED = "returned"
    ERRORED = "errored"
    NOT_STARTED = "not_started"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class Confinement:
    """What a run may do: the built-in tools that exist for it at all, and the settings
    that bound them. Decided by Relay for the role, never by the agent definition."""

    tools: tuple[str, ...]
    settings: Mapping[str, Any]


@dataclass(frozen=True)
class Request:
    # Chosen by Relay before the call, and used as the provider's session id.
    invocation_id: uuid.UUID
    definition: AgentDefinition
    model: str
    briefing: str
    schema: Mapping[str, Any]
    workdir: Path
    confinement: Confinement
    timeout: float
    effort: str | None = None
    budget_usd: Decimal | None = None


@dataclass(frozen=True)
class ProviderResult:
    outcome: Outcome
    terminal: Terminal | None = None
    reason: str = ""
    process_group: int | None = None
    started: Started | None = None


@dataclass(frozen=True)
class Capabilities:
    version: str
    flags: frozenset[str] = field(default_factory=frozenset)


def scrubbed(environment: Mapping[str, str]) -> dict[str, str]:
    """An environment to hand to a run: the given one without what is the supervisor's."""
    return {name: value for name, value in environment.items() if not _WITHHELD.fullmatch(name)}


class ClaudeAdapter:
    name = "claude"

    def __init__(
        self,
        binary: str = "claude",
        environment: Mapping[str, str] | None = None,
        grace: float = 2.0,
    ) -> None:
        self._binary = binary
        self._environment = dict(scrubbed(os.environ) if environment is None else environment)
        # How long a run that has reported its result is given to exit by itself.
        self._grace = grace
        self._capabilities: Capabilities | None = None

    async def capabilities(self) -> Capabilities:
        """What the installed CLI says of itself. Asked once."""
        if self._capabilities is None:
            version = await self._ask("--version")
            flags = frozenset(re.findall(r"--[a-z][a-z-]*", await self._ask("--help")))
            self._capabilities = Capabilities(version.strip(), flags)
        return self._capabilities

    async def _ask(self, flag: str) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                self._binary,
                flag,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._environment,
            )
        except OSError as failure:
            raise ProviderUnavailable(f"the Claude CLI could not be run: {failure}") from None
        answered, _ = await process.communicate()
        if process.returncode != 0:
            raise ProviderUnavailable(f"the Claude CLI did not answer {flag}")
        return answered.decode(errors="replace")

    def command(self, request: Request) -> list[str]:
        tools = list(request.confinement.tools)
        definition = {
            request.definition.name: {
                "description": request.definition.description,
                "prompt": request.definition.prompt,
                # The role's tools, whatever the definition's own file asked for.
                "tools": tools,
            }
        }
        return [
            self._binary,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--agents", json.dumps(definition),
            "--agent", request.definition.name,
            "--model", request.model,
            "--json-schema", json.dumps(dict(request.schema)),
            "--session-id", str(request.invocation_id),
            "--tools", ",".join(tools),
            "--settings", json.dumps(dict(request.confinement.settings)),
            # Nothing is taken from the user's, the project's or the working directory's
            # settings, servers, skills or commands.
            "--setting-sources", "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--permission-prompts", "none",
            "--no-session-persistence",
            *(["--effort", request.effort] if request.effort is not None else []),
            *(["--max-budget-usd", str(request.budget_usd)] if request.budget_usd is not None else []),
        ]  # fmt: skip

    async def invoke(
        self,
        request: Request,
        *,
        on_event: Callable[[Event], None] | None = None,
        on_start: Callable[[int], None] | None = None,
        checked: bool = True,
    ) -> ProviderResult:
        if checked:
            await self._require_flags(request)
        with tempfile.TemporaryFile() as complaints:
            try:
                process = await asyncio.create_subprocess_exec(
                    *self.command(request),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=complaints,
                    cwd=request.workdir,
                    env=self._environment,
                    # A group of its own: whatever it starts is ended with it.
                    start_new_session=True,
                    limit=_ONE_LINE_LIMIT,
                )
            except OSError as failure:
                return ProviderResult(
                    Outcome.NOT_STARTED, reason=f"could not be launched: {failure}"
                )
            group = process.pid
            if on_start is not None:
                on_start(group)
            started: Started | None = None
            terminal: Terminal | None = None
            reason = ""
            try:
                try:
                    async with asyncio.timeout(request.timeout):
                        await _say(process, request.briefing)
                        assert process.stdout is not None
                        async for raw in process.stdout:
                            for event in read_line(raw.decode(errors="replace")):
                                if on_event is not None:
                                    on_event(event)
                                if isinstance(event, Started):
                                    started = event
                                    reason = _beyond(event, request.confinement)
                                elif isinstance(event, Terminal):
                                    terminal = event
                            if terminal is not None or reason:
                                break
                except TimeoutError:
                    reason = f"no result within {request.timeout} seconds"
                if terminal is not None:
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(self._grace):
                            await process.wait()
            finally:
                # Whatever happened, including the caller being cancelled: nothing of
                # this run is left running.
                await _end(process, group)
            if terminal is not None:
                outcome = Outcome.RETURNED if terminal.succeeded else Outcome.ERRORED
                return ProviderResult(outcome, terminal, process_group=group, started=started)
            complaints.seek(0)
            said = complaints.read()[-2000:].decode(errors="replace").strip()
            reason = reason or "the run ended without reporting a result"
            return ProviderResult(
                Outcome.UNCERTAIN,
                reason=f"{reason}: {said}" if said else reason,
                process_group=group,
                started=started,
            )

    async def _require_flags(self, request: Request) -> None:
        offered = (await self.capabilities()).flags
        wanted = [
            *_ALWAYS_PASSED,
            *(["--effort"] if request.effort is not None else []),
            *(["--max-budget-usd"] if request.budget_usd is not None else []),
        ]
        missing = [flag for flag in wanted if flag not in offered]
        if missing:
            # A flag that is not there is not left out quietly: what Relay records as
            # asked for must be what was asked for.
            raise ProviderUnavailable(
                f"the installed Claude CLI does not offer {', '.join(missing)}"
            )


async def _say(process: asyncio.subprocess.Process, briefing: str) -> None:
    assert process.stdin is not None
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        process.stdin.write(briefing.encode())
        await process.stdin.drain()
    process.stdin.close()


def _beyond(started: Started, confinement: Confinement) -> str:
    """What the run has that it was not given, as a reason to end it; empty if nothing."""
    allowed = {*confinement.tools, _STRUCTURED_OUTPUT}
    tools = sorted(set(started.tools) - allowed)
    if not tools and not started.mcp_servers:
        return ""
    return (
        "the run was not confined as asked: it has "
        f"tools {tools} and servers {sorted(started.mcp_servers)} it was not given"
    )


async def _end(process: asyncio.subprocess.Process, group: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group, signal.SIGKILL)
    # Shielded: a caller cancelled twice must still not leave the process unreaped.
    await asyncio.shield(process.wait())
