"""The server Relay starts for the Product Owner's session (3C-1.10).

It speaks the Model Context Protocol over standard input and output: one JSON-RPC message
a line. It is bound to one project and one conversation, and what it offers is all there
is: the reading tools, `context`, `prepare`, and the three that keep what the Interpreter
works out. Each is a typed tool with a schema; none takes a command line. There is no
deciding operation here, no passphrase operation and no `doctor`: a model that asks for
one is told it does not exist, because here it does not.

Written against the protocol's published schema (revisions 2024-11-05, 2025-03-26 and
2025-06-18) with no dependency. What of it was verified against the real CLI is in the
step's record.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TextIO

from relay.application import conversations, prepared
from relay.application.inbox import Inbox
from relay.application.usage import NoSuchFeature, live_usage
from relay.cli import deciding, reading
from relay.persistence.database import Pool
from relay.runtime import Relay

NAME = "relay"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
THE_INTERPRETER = "the Interpreter, in the Product Owner's session"

_NO_SUCH_METHOD, _BAD_PARAMS, _BAD_REQUEST = -32601, -32602, -32600

_TEXT = {"type": "string", "minLength": 1}
_FEATURE = {"type": "string", "description": "A feature's name, such as F1."}


def _schema(required: tuple[str, ...] = (), **properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]


TOOLS = (
    Tool(
        "status",
        "Each feature and where it stands, with its stories and behaviours; what work is "
        "owed and to whom; and how much is waiting for the Product Owner.",
        _schema(),
    ),
    Tool(
        "inbox",
        "Everything waiting for the Product Owner, each with its handle and what can be "
        "done about it.",
        _schema(),
    ),
    Tool(
        "show",
        "One item waiting for the Product Owner, in full, as they will be shown it.",
        _schema(("item",), item={"type": "string", "description": "The item's handle."}),
    ),
    Tool(
        "roadmap",
        "A feature's roadmap in force and any proposal waiting.",
        _schema(feature=_FEATURE),
    ),
    Tool(
        "ledger",
        "Relay's record of what happened, in order.",
        _schema(
            subject={"type": "string", "description": "Only events about this subject."},
            since={"type": "integer", "minimum": 0, "description": "Only events after this."},
        ),
    ),
    Tool(
        "costs",
        "What calls have used and cost, as known now: for the project, or for one feature. "
        "Relay's own figures are estimates and are labelled so.",
        _schema(feature=_FEATURE),
    ),
    Tool(
        "context",
        "What this conversation needs to carry on, from Relay's record: features, the "
        "decisions in force and who made them, open questions, what is waiting, what was "
        "prepared and not yet confirmed, the latest analysis, and the notes with the "
        "Product Owner's answers. Read this first.",
        _schema(),
    ),
    Tool(
        "prepare",
        "Record an action for the Product Owner to confirm. It decides nothing. The "
        "Product Owner confirms it at a terminal of their own with the code this returns.",
        _schema(
            ("action", "of", "why"),
            action={"type": "string", "enum": [each for each in deciding.VERBS if each != "start"]},
            of={"type": "string", "description": "What it is done to: a handle, or a name."},
            text={"type": "string", "description": "The words the action carries, if it has any."},
            reason={"type": "string", "description": "The reason the action carries, if any."},
            why=_TEXT | {"description": "Why you propose it, for the Product Owner to read."},
        ),
    ),
    Tool(
        "record_analysis",
        "Keep a version of your analysis of the problem, for a feature or before one "
        "exists. It is recorded as written by a model and decides nothing.",
        _schema(("text",), text=_TEXT, feature=_FEATURE),
    ),
    Tool(
        "note_question",
        "Keep a question you mean to put to the Product Owner in this conversation. It is "
        "not a workflow question and nothing waits on it.",
        _schema(("text",), text=_TEXT, feature=_FEATURE),
    ),
    Tool(
        "note_answered",
        "Say which note the Product Owner answered: with their last message, or in an "
        "earlier turn that `context` lists beside the note. Relay records their own words "
        "of that turn against it, as they typed them.",
        _schema(
            ("note",),
            note={"type": "string", "description": "The note's identity."},
            said_in={
                "type": "string",
                "description": "The turn they answered it in, as `context` lists it beside "
                "the note, if it was an earlier one than their last message.",
            },
        ),
    ),
)


class Refused(Exception):
    """A tool was asked for something it will not do, and says why."""


def _check(tool: Tool, arguments: Any) -> dict[str, Any]:
    """The arguments, held to the tool's schema: what it does not name is not taken."""
    if not isinstance(arguments, dict):
        raise Refused(f"{tool.name} takes an object of arguments")
    properties: dict[str, Any] = tool.schema["properties"]
    for name in arguments:
        if name not in properties:
            raise Refused(f"{tool.name} takes no {name!r}")
    for name in tool.schema["required"]:
        if name not in arguments:
            raise Refused(f"{tool.name} needs {name!r}")
    for name, value in arguments.items():
        kind = properties[name]["type"]
        if kind == "string":
            if not isinstance(value, str):
                raise Refused(f"{tool.name}: {name!r} is text")
            if properties[name].get("minLength") and not value.strip():
                raise Refused(f"{tool.name}: {name!r} says something")
            allowed = properties[name].get("enum")
            if allowed is not None and value not in allowed:
                raise Refused(f"{tool.name}: {name!r} is one of {', '.join(allowed)}")
        elif kind == "integer" and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise Refused(f"{tool.name}: {name!r} is a whole number, nought or more")
    return arguments


class Server:
    """One project's reading tools and `prepare`, for one conversation."""

    def __init__(self, relay: Relay, project_id: str, conversation: uuid.UUID) -> None:
        self._relay = relay
        self._pool: Pool = relay.pool
        self._project = project_id
        self._conversation = conversation
        self._tools: dict[str, tuple[Tool, Callable[..., Awaitable[Any]]]] = {
            each.name: (each, getattr(self, f"_{each.name}")) for each in TOOLS
        }

    async def call(self, name: str, arguments: Any) -> Any:
        tool, do = self._tools[name]
        return await do(**_check(tool, {} if arguments is None else arguments))

    async def _status(self) -> Any:
        return await reading.status(self._pool, self._project)

    async def _inbox(self) -> Any:
        return [reading.item_facts(each) for each in await Inbox(self._pool).items(self._project)]

    async def _show(self, item: str) -> Any:
        found = await Inbox(self._pool).item(self._project, item)
        if found is None:
            raise Refused(f"nothing waiting in {self._project} is {item!r}: see `inbox`")
        return reading.item_facts(found)

    async def _roadmap(self, feature: str | None = None) -> Any:
        try:
            return await reading.roadmap(self._pool, self._project, feature)
        except LookupError as none:
            raise Refused(str(none)) from none

    async def _ledger(self, subject: str | None = None, since: int = 0) -> Any:
        return await reading.ledger(self._pool, self._project, subject, since)

    async def _costs(self, feature: str | None = None) -> Any:
        try:
            return await live_usage(self._pool, self._project, feature)
        except NoSuchFeature as none:
            raise Refused(str(none)) from none

    async def _context(self) -> Any:
        return await conversations.context(self._pool, self._project)

    async def _prepare(
        self, action: str, of: str, why: str, text: str | None = None, reason: str | None = None
    ) -> Any:
        try:
            # Resolved now, against the record as it stands, exactly as `relay prepare`
            # does: what cannot be done is refused here, and what can is kept with the
            # record as it was shown.
            decision = await deciding.resolve(self._relay, self._project, action, of, text, reason)
            made = await prepared.prepare(
                self._pool, self._project, action, decision.target_id or of, text, reason, why,
                decision.shown, decision.basis, by=THE_INTERPRETER,
            )  # fmt: skip
        except (deciding.CannotDecide, LookupError, ValueError) as no:
            raise Refused(str(no)) from no
        return {
            "prepared": made.code,
            "decided": "nothing",
            "next": (
                "The Product Owner confirms it at a terminal of their own, within the hour: "
                f"`relay confirm {made.code}`. Tell them the code and what it would do."
            ),
        }

    async def _record_analysis(self, text: str, feature: str | None = None) -> Any:
        try:
            version = await conversations.record_analysis(
                self._pool, self._conversation, text, feature
            )
        except (conversations.NotThisConversation, ValueError) as no:
            raise Refused(str(no)) from no
        return {"analysis": version, "decided": "nothing", "written": conversations.WRITTEN_BY}

    async def _note_question(self, text: str, feature: str | None = None) -> Any:
        try:
            note = await conversations.note_question(self._pool, self._conversation, text, feature)
        except (conversations.NotThisConversation, ValueError) as no:
            raise Refused(str(no)) from no
        return {"note": str(note), "decided": "nothing"}

    async def _note_answered(self, note: str, said_in: str | None = None) -> Any:
        try:
            words = await conversations.note_answered(self._pool, self._conversation, note, said_in)
        except conversations.NotThisConversation as no:
            raise Refused(str(no)) from no
        return {"note": note, "recorded_as_the_persons_answer": words}

    async def answer(self, message: Any) -> dict[str, Any] | None:
        """The reply to one message of the protocol; None to a notification."""
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            return _error(None, _BAD_REQUEST, "not a request")
        method, asked = message["method"], message.get("id")
        if "id" not in message:
            return None  # a notification is answered with nothing, whatever it says
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "initialize":
            wanted = params.get("protocolVersion")
            return _result(
                asked,
                {
                    "protocolVersion": wanted if wanted in PROTOCOLS else PROTOCOLS[0],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": NAME, "version": "3"},
                },
            )
        if method == "ping":
            return _result(asked, {})
        if method == "tools/list":
            return _result(
                asked,
                {
                    "tools": [
                        {
                            "name": each.name,
                            "description": each.description,
                            "inputSchema": each.schema,
                        }
                        for each in TOOLS
                    ]
                },
            )
        if method == "tools/call":
            name = params.get("name")
            if name not in self._tools:
                # It does not exist here: there is nothing behind any other name.
                return _error(asked, _BAD_PARAMS, f"there is no tool {name!r} on this server")
            try:
                done = await self.call(name, params.get("arguments"))
            except Refused as refused:
                return _result(asked, _said(str(refused), is_error=True))
            return _result(asked, _said(json.dumps(done, indent=2, default=str)))
        return _error(asked, _NO_SUCH_METHOD, f"this server has no {method!r}")


def _said(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _result(asked: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": asked, "result": result}


def _error(asked: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": asked, "error": {"code": code, "message": message}}


async def serve(server: Server, lines: TextIO | None = None, out: TextIO | None = None) -> None:
    """Answer messages, one a line, until the input ends. Nothing but the protocol is
    written to the output: it is the client's to parse."""
    source, sink = lines or sys.stdin, out or sys.stdout
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, source.readline)
        if not line:
            return
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply: dict[str, Any] | None = _error(None, -32700, "not JSON")
        else:
            try:
                reply = await server.answer(message)
            except Exception as failure:  # noqa: BLE001 - one bad call does not end the session
                asked = message.get("id") if isinstance(message, dict) else None
                reply = _error(asked, -32603, f"{type(failure).__name__}: {failure}")
        if reply is not None:
            sink.write(json.dumps(reply) + "\n")
            sink.flush()
