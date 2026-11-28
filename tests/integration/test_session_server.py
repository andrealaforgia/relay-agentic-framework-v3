"""The server Relay starts for the Product Owner's session (3C-1.10, 3C-1.11, 3C-2.9)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
import redis.asyncio as redis
from psycopg_pool import AsyncConnectionPool

from relay.application import authority, conversations, prepared
from relay.git.checkout import name_attempts_under
from relay.runtime import assemble, configure
from relay.session.server import PROTOCOLS, THE_INTERPRETER, TOOLS, Server, serve
from tests.integration.completion_world import CompletionWorld
from tests.integration.story_world import STANDARD_RATE
from tests.integration.test_deciding_commands import PASSPHRASE, Desk
from tests.integration.trunk_world import PROJECT


class Session:
    """A session's side of the server: what a client of the protocol sends and is sent."""

    def __init__(self, desk: Desk, server: Server) -> None:
        self.desk, self.world, self.server, self.sent = desk, desk.world, server, 0

    async def ask(self, method: str, **params: Any) -> dict[str, Any]:
        self.sent += 1
        reply = await self.server.answer(
            {"jsonrpc": "2.0", "id": self.sent, "method": method, "params": params}
        )
        assert reply is not None and reply["id"] == self.sent and reply["jsonrpc"] == "2.0"
        return reply

    async def call(self, tool: str, **arguments: Any) -> Any:
        """A tool's result, read as the JSON it is given as."""
        result = (await self.ask("tools/call", name=tool, arguments=arguments))["result"]
        [said] = result["content"]
        assert result["isError"] is False, said["text"]
        return json.loads(said["text"])

    async def refused(self, tool: str, **arguments: Any) -> str:
        result = (await self.ask("tools/call", name=tool, arguments=arguments))["result"]
        assert result["isError"] is True
        return str(result["content"][0]["text"])


@pytest.fixture
async def session(
    database: AsyncConnectionPool,
    broker: redis.Redis,
    shell: dict[str, str],
    tmp_path: Path,
    runner_image: str,
) -> Any:
    world = CompletionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await configure(
        database,
        workspaces=str(tmp_path.resolve() / "workspaces"),
        mirrors=str(world.mirrors),
        image=runner_image,
    )
    await authority.set_first(database, PASSPHRASE)
    relay = await assemble(database, broker)
    talk = await conversations.open_conversation(database, PROJECT)
    yield Session(Desk(world, shell), Server(relay, PROJECT, talk))
    name_attempts_under("")


async def another(session: Session, broker: redis.Redis) -> Session:
    """A new session of the same project: a new conversation, and nothing of the old one's."""
    pool = session.world.database
    talk = await conversations.open_conversation(pool, PROJECT)
    return Session(session.desk, Server(await assemble(pool, broker), PROJECT, talk))


async def test_the_server_offers_exactly_its_tools_and_nothing_that_decides(
    session: Session,
) -> None:
    listed = (await session.ask("tools/list"))["result"]["tools"]

    assert [each["name"] for each in listed] == [
        "status", "inbox", "show", "roadmap", "ledger", "costs", "context", "prepare",
        "record_analysis", "note_question", "note_answered",
    ]  # fmt: skip
    for each in listed:
        assert each["description"]
        assert each["inputSchema"]["type"] == "object"
        assert each["inputSchema"]["additionalProperties"] is False  # nothing untyped
        assert "command" not in each["inputSchema"]["properties"]  # none takes a command line
    prepare = next(each for each in listed if each["name"] == "prepare")
    assert "start" not in prepare["inputSchema"]["properties"]["action"]["enum"]
    # What decides, confirms, or changes the installation does not exist here.
    for absent in ("approve", "confirm", "answer", "passphrase", "doctor", "policy", "Bash"):
        reply = await session.ask("tools/call", name=absent, arguments={})
        assert reply["error"]["code"] == -32602 and "there is no tool" in reply["error"]["message"]
    assert len(TOOLS) == 11


async def test_the_server_speaks_the_protocol_and_no_more(session: Session) -> None:
    for known in PROTOCOLS:
        started = (await session.ask("initialize", protocolVersion=known, capabilities={}))[
            "result"
        ]
        assert started["protocolVersion"] == known
        assert started["capabilities"] == {"tools": {}} and started["serverInfo"]["name"] == "relay"
    newer = (await session.ask("initialize", protocolVersion="2099-01-01"))["result"]
    assert newer["protocolVersion"] == PROTOCOLS[0]  # one it does speak
    assert (await session.ask("ping"))["result"] == {}
    assert (await session.ask("resources/list"))["error"]["code"] == -32601
    # A notification is answered with nothing; what is no request is said to be none.
    notified = await session.server.answer(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert notified is None
    for nonsense in ([1, 2], {"id": 1}, "hello"):
        reply = await session.server.answer(nonsense)
        assert reply is not None and reply["error"]["code"] == -32600


async def test_every_reading_tool_reads_the_record_of_its_own_project(session: Session) -> None:
    world = session.world
    gate = await world.roadmap(STANDARD_RATE)

    status = await session.call("status")
    [item] = await session.call("inbox")
    shown = await session.call("show", item=item["handle"])
    roadmap = await session.call("roadmap", feature="F1")
    ledger = await session.call("ledger")
    later = await session.call("ledger", since=ledger[-1]["sequence"])
    costs = await session.call("costs")
    context = await session.call("context")

    assert status == session.desk.read("status")  # the same facts the command gives
    assert item == session.desk.read("inbox")[0] and str(gate).startswith(item["handle"])
    assert shown == session.desk.read("show", item["handle"])
    assert roadmap == session.desk.read("roadmap", "F1")
    assert [each["event"] for each in ledger][:2] == ["project.opened", "feature.opened"]
    assert later == []
    assert costs == session.desk.read("costs")
    assert context["project"] == PROJECT and context["features"][0]["feature"] == "F1"
    assert context["waiting_for_the_product_owner"] == [item]
    assert (await session.call("costs", feature="F1"))["feature"] == "F1"
    assert await session.call("roadmap") == roadmap  # the one feature there is


async def test_a_tool_takes_only_what_its_schema_names(session: Session) -> None:
    world = session.world
    gate = await world.roadmap(STANDARD_RATE)

    assert "takes no 'command'" in await session.refused("status", command="rm -rf /")
    assert "needs 'item'" in await session.refused("show")
    assert "'item' is text" in await session.refused("show", item=7)
    assert "nothing waiting" in await session.refused("show", item="ffffffff")
    assert "whole number" in await session.refused("ledger", since=-1)
    assert "whole number" in await session.refused("ledger", since=True)
    assert "there is no feature 'F9'" in await session.refused("costs", feature="F9")
    assert "is one of approve" in await session.refused(
        "prepare", action="start", of="a feature", why="because"
    )
    assert "'why' says something" in await session.refused(
        "prepare", action="approve", of=str(gate), why="   "
    )
    assert "is not something to answer" in await session.refused(
        "prepare", action="answer", of=str(gate), text="yes", why="because"
    )
    assert "'text' says something" in await session.refused("record_analysis", text=" ")
    assert "there is no feature 'F9'" in await session.refused(
        "note_question", text="Why?", feature="F9"
    )
    result = (await session.ask("tools/call", name="status", arguments=[1]))["result"]
    assert result["isError"] is True and "takes an object" in result["content"][0]["text"]
    # None of it prepared or recorded anything.
    assert await world.column("SELECT count(*) FROM prepared_actions") == [0]
    assert await world.column("SELECT count(*) FROM analyses") == [0]
    assert await world.column("SELECT count(*) FROM notes") == [0]


async def test_a_session_prepares_and_a_new_session_and_the_person_find_it(
    session: Session, broker: redis.Redis
) -> None:
    """3C-1.11, the acceptance history: a session reads a real gate and prepares its
    approval; it is closed; a new session's context shows the gate and the prepared
    action; and the Product Owner confirms it at a terminal of their own."""
    world = session.world
    gate = await world.roadmap(STANDARD_RATE)
    [item] = await session.call("inbox")

    made = await session.call(
        "prepare", action="approve", of=item["handle"], why="it is the roadmap that was asked for"
    )

    assert made["decided"] == "nothing" and f"relay confirm {made['prepared']}" in made["next"]
    assert await world.column("SELECT state FROM gates") == ["open"]  # nothing decided
    await conversations.close_conversation(world.database, session.server._conversation)
    new = await another(session, broker)
    told = await new.call("context")
    [waiting] = told["waiting_for_the_product_owner"]
    [action] = told["prepared_and_not_confirmed"]
    assert str(gate).startswith(waiting["handle"])
    assert (action["code"], action["action"], action["of"]) == (
        made["prepared"],
        "approve",
        str(gate),
    )
    assert action["why"] == "it is the roadmap that was asked for"
    assert action["prepared_by"] == THE_INTERPRETER

    confirmed = session.desk.po("confirm", made["prepared"])

    assert confirmed.code == 0, confirmed.printed
    assert "it is the roadmap that was asked for" in confirmed.shown
    assert await world.column("SELECT state FROM gates") == ["decided"]
    assert await world.column("SELECT outcome FROM gates") == ["approved"]
    assert (await new.call("context"))["prepared_and_not_confirmed"] == []


async def test_an_analysis_and_a_question_outlive_the_session_that_made_them(
    session: Session, broker: redis.Redis
) -> None:
    """3C-2.9, the acceptance history: an analysis revision and an unanswered note; the
    session is closed before anything is prepared; a new session's context has both."""
    world = session.world
    first = await session.call("record_analysis", text="VAT is added twice on export.")
    revised = await session.call("record_analysis", text="VAT is added twice, on every sale.")
    noted = await session.call("note_question", text="Is it shown net or gross today?")

    assert (first["analysis"], revised["analysis"]) == (1, 2)
    assert first["decided"] == noted["decided"] == "nothing"
    await conversations.close_conversation(world.database, session.server._conversation)
    new = await another(session, broker)
    told = await new.call("context")
    assert told["analysis"]["version"] == 2
    assert told["analysis"]["text"] == "VAT is added twice, on every sale."
    assert told["analysis"]["written"] == conversations.WRITTEN_BY
    [note] = told["notes"]
    assert (note["note"], note["question"], note["outstanding"]) == (
        noted["note"],
        "Is it shown net or gross today?",
        True,
    )
    assert told["prepared_and_not_confirmed"] == [] and told["features"] == []

    # 3C-3.7: the person answers in the new session; the model only names the note.
    await conversations.begin_turn(
        world.database, new.server._conversation, "Gross, since March.", "claude-haiku-5-5"
    )
    answered = await new.call("note_answered", note=note["note"])
    assert answered["recorded_as_the_persons_answer"] == "Gross, since March."
    [after] = (await new.call("context"))["notes"]
    assert after["outstanding"] is False
    assert after["answer_in_the_persons_words"] == "Gross, since March."
    assert "has its answer already" in await new.refused("note_answered", note=note["note"])


async def test_context_gives_a_returning_person_the_state_the_commands_give(
    session: Session,
) -> None:
    """3C-S4-1.7: a paused feature is paused; and a confirmation that was begun and then
    decided nothing is still there to be finished, however long ago it was prepared."""
    world, desk = session.world, session.desk
    desk.shell["RELAY_PROJECT"] = PROJECT
    gate = await world.roadmap(STANDARD_RATE)
    made = await session.call("prepare", action="approve", of=str(gate), why="as asked")
    code = made["prepared"]
    # The confirming command took the code and died before it decided anything.
    assert await prepared.consume(world.database, code, f"confirm:{code}")
    async with world.database.connection() as connection:
        await connection.execute(
            "UPDATE prepared_actions SET expires_at = now() - interval '2 hours'"
        )

    told = await session.call("context")

    [begun] = told["prepared_and_not_confirmed"]
    assert begun["code"] == code and f"`relay confirm {code}` again finishes it" in begun["state"]
    finished = desk.po("confirm", code)
    assert finished.code == 0, finished.printed + finished.shown
    assert (await session.call("context"))["prepared_and_not_confirmed"] == []
    # And a feature that was paused is given as paused, as `relay status` gives it.
    assert desk.po("pause", "F1", "--reason", "waiting for the budget").code == 0
    [feature] = (await session.call("context"))["features"]
    assert feature["state"] == desk.read("status")["features"][0]["state"] == "paused"


async def test_what_is_written_to_the_client_is_the_protocol_and_nothing_else(
    session: Session,
) -> None:
    sent = io.StringIO(
        "\n".join(
            [
                "this is not JSON",
                "",
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": "a", "method": "ping"}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "status"},
                    }
                ),
            ]
        )
        + "\n"
    )
    out = io.StringIO()

    await serve(session.server, sent, out)

    lines = [json.loads(each) for each in out.getvalue().splitlines()]
    assert [each.get("id") for each in lines] == [None, "a", 2]
    assert lines[0]["error"]["code"] == -32700 and lines[1]["result"] == {}
    assert json.loads(lines[2]["result"]["content"][0]["text"])["features"] == []
