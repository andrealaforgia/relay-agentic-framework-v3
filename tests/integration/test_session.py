"""`relay session`: the Product Owner's conversation, hosted by Relay (3C-2.8, 3C-3.6 to 3.8)."""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application import authority, conversations
from relay.application.usage import live_usage
from relay.git.checkout import name_attempts_under
from relay.runtime import configure
from relay.session import host
from tests.integration.completion_world import CompletionWorld
from tests.integration.person import at_a_terminal, with_no_terminal
from tests.integration.story_world import STANDARD_RATE
from tests.integration.test_deciding_commands import PASSPHRASE, Desk
from tests.integration.trunk_world import PROJECT

STAND_IN = Path(__file__).resolve().parent.parent / "fixtures" / "claude" / "session_claude.py"
MODEL = "claude-haiku-5-5"


class Talk:
    """A session of the project with a scripted stand-in for the CLI behind it."""

    def __init__(self, desk: Desk, home: Path, cli: Path) -> None:
        self.desk, self.world, self.home, self.cli = desk, desk.world, home, cli
        self.heard: list[str] = []
        self.launch = host.Launch(
            binary=str(cli),
            model=MODEL,
            budget_usd=Decimal("0.50"),
            timeout=60.0,
            server=[sys.executable, "-m", "relay.cli.main", "session-server", "--project", PROJECT],
            server_environment={k: v for k, v in desk.shell.items() if k.startswith("RELAY_")},
            workdir=home / "work",
            environment={**os.environ, "SESSION_STANDIN": str(home)},
        )

    def script(self, *rules: dict[str, Any]) -> None:
        (self.home / "script.json").write_text(json.dumps(list(rules)))

    async def say(self, *lines: str | Callable[[], str]) -> int:
        """One session: the person types these, then ends it. A line may be something
        that happens first, while the person is thinking."""
        pending = list(lines)

        def listen() -> str | None:
            if not pending:
                return None
            line = pending.pop(0)
            return line() if callable(line) else line

        return await host.converse(
            self.world.database, PROJECT, self.launch, listen=listen, say=self.heard.append
        )

    def launched(self) -> list[dict[str, Any]]:
        file = self.home / "launched.jsonl"
        return [json.loads(each) for each in file.read_text().splitlines()] if file.exists() else []

    async def turns(self) -> list[dict[str, Any]]:
        async with self.world.database.connection() as connection:
            return [
                dict(each)
                for each in await (
                    await connection.execute(
                        "SELECT * FROM conversation_turns ORDER BY started_at, number"
                    )
                ).fetchall()
            ]


def read(heard: list[str], having: str) -> dict[str, Any]:
    """The last thing a tool returned, among what the session was heard to say, that has
    this in it: a reply here is the results of the tools it called, one after another."""
    decoder, found = json.JSONDecoder(), None
    for said in heard:
        at = said.find("{")
        while 0 <= at < len(said):
            try:
                value, end = decoder.raw_decode(said, at)
            except ValueError:
                break
            if isinstance(value, dict) and having in value:
                found = value
            at = said.find("{", end)
    assert found is not None, heard
    return found


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


@pytest.fixture
async def talk(
    database: AsyncConnectionPool, shell: dict[str, str], tmp_path: Path, runner_image: str
) -> Any:
    world = CompletionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    home = tmp_path / "stand-in"
    home.mkdir()
    cli = home / "claude"
    cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STAND_IN}" "$@"\n')
    cli.chmod(0o755)
    await configure(
        database,
        workspaces=str(tmp_path.resolve() / "workspaces"),
        mirrors=str(world.mirrors),
        image=runner_image,
        cli=str(cli),
        profiles={"default": {"model": MODEL, "timeout": 60}},
    )
    await authority.set_first(database, PASSPHRASE)
    async with database.connection() as connection:
        await connection.execute(
            "UPDATE installation SET policy = jsonb_build_object('statement', 'none: a test')"
        )
    shell["SESSION_STANDIN"] = str(home)
    yield Talk(Desk(world, shell), home, cli)
    name_attempts_under("")


async def test_a_conversation_is_turns_of_one_provider_session_each_a_call_on_record(
    talk: Talk,
) -> None:
    """3C-2.8 and 3C-3.6: a line from the person, one call with Relay's server and no
    other tool, continuing the same provider session; each turn a call of its own."""
    talk.script(
        {
            "when": "where are we",
            "calls": [{"tool": "status"}],
            "say": "Turn {turns}: nothing yet.",
        },
        {"when": "and now", "say": "Turn {turns}: still nothing."},
    )

    assert await talk.say("where are we?", "   ", "and now?") == 2  # an empty line is no turn

    first, second = talk.launched()
    for each in (first, second):
        argv = each["argv"]
        assert flag(argv, "--tools") == "" and "--strict-mcp-config" in argv
        assert flag(argv, "--setting-sources") == "" and "--disable-slash-commands" in argv
        assert flag(argv, "--allowedTools").split(",") == list(host.GIVEN)
        assert flag(argv, "--max-budget-usd") == "0.50" and flag(argv, "--model") == MODEL
        assert "--no-session-persistence" not in argv  # the session is to be continued
        assert "decide nothing" in flag(argv, "--system-prompt")
    # The first is a new provider session, told where things stand from the record; the
    # second continues it and is told only what the person said.
    assert not first["resumed"] and second["resumed"] and second["session"] == first["session"]
    assert first["told"].endswith("where are we?") and '"features": []' in first["told"]
    assert second["told"] == "and now?"
    assert "Turn 1: nothing yet." in talk.heard and "Turn 2: still nothing." in talk.heard
    one, two = await talk.turns()
    assert (one["number"], one["said"], one["state"], one["continued"]) == (
        1,
        "where are we?",
        "applied",
        False,
    )
    assert (two["number"], two["said"], two["state"], two["continued"]) == (
        2,
        "and now?",
        "applied",
        True,
    )
    assert one["turn_id"] != two["turn_id"] and one["conversation_id"] == two["conversation_id"]
    assert str(one["provider_session"]) == first["session"] == str(two["provider_session"])
    assert one["reply"] == "Turn 1: nothing yet." and one["provider_cost_usd"] == Decimal("0.002")
    assert one["usage"][0]["model"] == MODEL and one["estimated_cost_usd"] is not None
    assert any("0.002 USD charged by the provider" in each for each in talk.heard)
    # As an amount, and not with the noise of the binary fraction it arrived as.
    noisy = host.Ran("applied", terminal=host.Terminal({"total_cost_usd": 0.0011075000000000002}))
    assert "0.0011075 USD charged by the provider" in host.cost_line(noisy, talk.launch.prices)
    assert any("an estimate, price table" in each for each in talk.heard)
    account = await live_usage(talk.world.database, PROJECT)
    assert account["by_role"]["interpreter"]["calls"] == 2
    assert await talk.world.column("SELECT closed_at IS NOT NULL FROM conversations") == [True]


async def test_a_session_reads_a_real_item_and_prepares_and_only_the_person_decides(
    talk: Talk,
) -> None:
    """3C-2.8, the acceptance test of the launch: a real item read through the server,
    and one action prepared; the Product Owner confirms it at a terminal of their own."""
    world = talk.world
    gate = await world.roadmap(STANDARD_RATE)
    talk.script(
        {"when": "what is waiting", "calls": [{"tool": "inbox"}], "say": "Waiting: {results}"},
        {
            "when": "prepare",
            "calls": [
                {
                    "tool": "prepare",
                    "arguments": {"action": "approve", "of": str(gate), "why": "as you asked"},
                }
            ],
            "say": "Prepared: {results}",
        },
    )

    await talk.say("what is waiting?", "prepare its approval")

    said = "\n".join(talk.heard)
    assert '"kind": "gate"' in said and str(gate)[:8] in said  # read from the record
    code = json.loads(said.split("Prepared: ")[1].split("\n[relay")[0])["prepared"]
    assert await world.column("SELECT state FROM gates") == ["open"]  # nothing decided
    assert await world.column("SELECT prepared_by FROM prepared_actions") == [
        "the Interpreter, in the Product Owner's session"
    ]
    confirmed = talk.desk.po("confirm", code)
    assert confirmed.code == 0, confirmed.printed
    assert "as you asked" in confirmed.shown
    assert await world.column("SELECT outcome FROM gates") == ["approved"]


async def test_a_run_that_starts_with_more_or_less_than_it_was_given_is_ended(talk: Talk) -> None:
    """3C-2.8: the inventory is observed on every turn, and held to Relay's server and its
    tools exactly."""
    talk.script(
        {"when": "a shell", "extra_tool": "Bash", "say": "I have a shell."},
        {"when": "no server", "no_server": True, "say": "I have no server."},
        {"when": "one short", "missing_tool": "prepare", "say": "I cannot prepare."},
        {
            "when": "eager",
            "say_first": "I speak before I say what I have.",
            "extra_tool": "Bash",
            "say": "Too late.",
        },
        {"when": "as given", "say": "As given."},
    )

    await talk.say("with a shell", "with no server", "one short", "eager", "as given")

    shell, bare, short, eager, good = await talk.turns()
    assert short["state"] == "uncertain"
    assert "without Relay's tools: mcp__relay__prepare" in short["reason"]
    # Nothing a run says is passed on before what it started with has been seen to be
    # what it was given.
    assert eager["state"] == "uncertain" and "I speak before I say what I have." not in talk.heard
    assert shell["state"] == "uncertain" and "tools it was not given: Bash" in shell["reason"]
    assert bare["state"] == "uncertain" and "not relay alone" in bare["reason"]
    assert (shell["reply"], bare["reply"]) == (None, None)
    assert good["state"] == "applied" and good["reply"] == "As given."
    assert "I have a shell." not in talk.heard and "I have no server." not in talk.heard
    assert any("did not complete" in each and "Bash" in each for each in talk.heard)
    assert any("this turn's cost is not known" in each for each in talk.heard)


async def test_a_turn_whose_result_is_lost_is_uncertain_and_the_cost_view_says_so(
    talk: Talk,
) -> None:
    """3C-3.6: two turns of one conversation; the terminal result of the second lost."""
    talk.script(
        {"when": "first", "say": "One."},
        {"when": "second", "lose_result": True, "say": "Two, and then silence."},
        {"when": "third", "fail": True, "say": "It went wrong."},
    )

    await talk.say("first", "second", "third")

    one, two, three = await talk.turns()
    assert (one["state"], two["state"], three["state"]) == ("applied", "uncertain", "errored")
    assert two["reason"] == "the run ended without a result" and two["provider_cost_usd"] is None
    # A call that ended in an error was a call all the same, and what it cost is kept.
    assert three["provider_cost_usd"] == Decimal("0.002") and three["usage"]
    # And its provider session, which its result named, is the conversation's still.
    assert await talk.world.column("SELECT provider_session FROM conversations") == [
        three["provider_session"]
    ]
    assert three["provider_session"] == one["provider_session"]
    account = await live_usage(talk.world.database, PROJECT)
    interpreter = account["by_role"]["interpreter"]
    assert (interpreter["calls"], interpreter["calls_whose_end_is_not_known"]) == (2, 1)
    assert interpreter["charged_by_the_provider"]["usd"] == "0.004"  # what is known
    costs = talk.desk.anyone("costs", "--project", PROJECT).out
    assert "the interpreter: 2 call(s), and 1 whose end is not known" in costs


async def test_a_turn_that_went_wrong_is_continued_from_all_the_same(talk: Talk) -> None:
    """The provider's session a failed turn named is there to be continued: what the
    person said in that turn is in it, and the next turn does not start again from nothing."""
    talk.script(
        {"when": "first", "fail": True, "say": "It went wrong."},
        {"when": "second", "say": "Turn {turns}."},
    )

    await talk.say("first", "second")

    failed, continued = talk.launched()
    assert continued["resumed"] and continued["session"] == failed["session"]
    assert "Turn 2." in talk.heard
    one, two = await talk.turns()
    assert (one["state"], two["state"], two["continued"]) == ("errored", "applied", True)


async def test_a_provider_session_that_cannot_be_resumed_is_not_the_end_of_the_conversation(
    talk: Talk,
) -> None:
    """3C-3.8: the provider's transcript is deleted between two turns, and the next turn
    recovers the analysis, the answered note and the prepared action from Relay's record."""
    world = talk.world
    gate = await world.roadmap(STANDARD_RATE)
    talk.script(
        {
            "when": "VAT is wrong",
            "calls": [
                {"tool": "record_analysis", "arguments": {"text": "VAT is added twice on export."}},
                {"tool": "note_question", "arguments": {"text": "Net or gross today?"}},
            ],
            "say": "Noted: {results}",
        },
        {"when": "Gross, since March", "say": "Thank you."},
        {"when": "carry on", "say": "Turn {turns} of this provider session."},
    )
    await talk.say("VAT is wrong")
    [note] = await world.column("SELECT note_id::text FROM notes")
    talk.script(
        # First, since a new provider session is told the record, which has the answer in it.
        {"when": "carry on", "say": "Turn {turns} of this provider session."},
        {
            "when": "Gross, since March",
            "calls": [
                {"tool": "note_answered", "arguments": {"note": str(note)}},
                {
                    "tool": "prepare",
                    "arguments": {"action": "approve", "of": str(gate), "why": "it fits"},
                },
            ],
            "say": "Recorded and prepared.",
        },
    )

    def and_the_transcript_is_lost() -> str:
        for each in (talk.home / "sessions").iterdir():
            each.unlink()
        return "carry on"

    await talk.say("Gross, since March.", and_the_transcript_is_lost)

    *_, answered, resumed, afresh = talk.launched()
    assert resumed["resumed"] and resumed["session"] == answered["session"]  # tried, and gone
    assert resumed["told"] is None  # it never got as far as being told anything
    assert not afresh["resumed"] and afresh["session"] != answered["session"]
    told = afresh["told"]
    assert told.endswith("carry on") and "VAT is added twice on export." in told
    assert '"answer_in_the_persons_words": "Gross, since March."' in told
    [code] = await world.column("SELECT code FROM prepared_actions")
    assert f'"code": "{code}"' in told
    assert "Turn 1 of this provider session." in talk.heard  # a new one, and it carried on
    *_, last = await talk.turns()
    assert (last["state"], last["continued"], last["said"]) == ("applied", False, "carry on")
    assert str(last["provider_session"]) == afresh["session"]
    # One turn, one call on record, though the CLI was run twice for it.
    assert len(await talk.turns()) == 3
    [kept] = await world.column(
        "SELECT provider_session::text FROM conversations ORDER BY opened_at DESC LIMIT 1"
    )
    assert kept == afresh["session"]


async def test_what_the_person_typed_is_kept_when_the_session_ends_before_the_model_replies(
    talk: Talk,
) -> None:
    """3C-3.7 and 3C-S4-1.5: the session is closed after the answer is recorded and before
    the model replies. The person comes back and says something else. A new session is
    told the note and each thing the person has said since, with the turn it was said
    in; it names that turn, and the note has the words of it and no others."""
    talk.script(
        {
            "when": "VAT",
            "calls": [{"tool": "note_question", "arguments": {"text": "Net or gross today?"}}],
            "say": "Net or gross today?",
        },
        {"when": "Gross, since March", "lose_result": True, "say": "(never heard)"},
        {"when": "hello again", "calls": [{"tool": "context"}], "say": "{results}"},
    )
    await talk.say("VAT is wrong", "Gross, since March.")

    await talk.say("hello again")

    *_, afresh = talk.launched()
    assert not afresh["resumed"]
    [note] = read(talk.heard, "notes")["notes"]
    assert note["outstanding"] is True and note["question"] == "Net or gross today?"
    first, second = note["said_by_the_person_since"]
    assert (first["said"], second["said"]) == ("Gross, since March.", "hello again")
    talk.script(
        {
            "when": "which one",
            "calls": [
                {
                    "tool": "note_answered",
                    "arguments": {"note": note["note"], "said_in": first["turn"]},
                },
                {"tool": "context"},
            ],
            "say": "{results}",
        }
    )
    await talk.say("which one did I answer?")
    [linked] = read(talk.heard, "notes")["notes"]
    assert linked["outstanding"] is False
    assert linked["answer_in_the_persons_words"] == "Gross, since March."  # not "hello again"
    assert await talk.world.column("SELECT answer FROM notes") == ["Gross, since March."]


async def test_a_resume_that_ends_without_saying_how_is_one_call_and_not_two(talk: Talk) -> None:
    """3C-S4-1.1: only the provider's own refusal of a session that is not there is a
    reason to make the turn again. A resumed run that vanishes may have been a call: it
    is left as one, uncertain, and nothing more is launched for the turn."""
    talk.script(
        {"when": "first", "say": "One."},
        {"when": "second", "vanish": True, "say": "(nothing)"},
        {"when": "third", "no_start": True, "say": "A result with no start."},
        # An error before the start that is not the provider saying it has no such session.
        {"when": "fourth", "refuse": {"saying": "Not logged in"}},
        # And one that says so, and says it cost something: then it was a call.
        {
            "when": "fifth",
            "refuse": {"saying": "No conversation found with session ID: x", "cost": 0.01},
        },
    )

    await talk.say("first", "second", "third", "fourth", "fifth")

    assert [each["resumed"] for each in talk.launched()] == [False, True, True, True, True]
    *_, fourth, fifth = await talk.turns()
    assert (fourth["state"], fourth["reason"]) == ("errored", "error_during_execution")
    assert fifth["state"] == "errored" and fifth["provider_cost_usd"] == Decimal("0.01")
    one, two, three, *_ = await talk.turns()
    assert one["state"] == "applied"
    assert (two["state"], two["reason"]) == ("uncertain", "the run ended without a result")
    # A result that came with no start: what it says it used is kept, what it said is
    # not passed on, and the turn is not made again.
    assert three["state"] == "errored" and three["provider_cost_usd"] == Decimal("0.002")
    assert three["reply"] is None and "A result with no start." not in talk.heard
    assert three["usage"] and three["continued"] is True
    costs = talk.desk.anyone("costs", "--project", PROJECT).out
    assert "the interpreter: 4 call(s), and 1 whose end is not known" in costs
    assert "charged by the provider: 0.014 USD over 4 call(s)" in costs


async def test_a_run_is_held_to_the_session_it_was_asked_to_be(talk: Talk) -> None:
    """3C-S4-1.2: a run that starts as another session is ended; a result of another
    session is not the turn's reply, and its session is not the one continued next."""
    other = "11111111-2222-4333-8444-555555555555"
    talk.script(
        {"when": "first", "say": "One."},
        {"when": "starts as another", "start_as": other, "say": "As another."},
        {"when": "ends as another", "end_as": other, "say": "Ended as another."},
        {"when": "last", "say": "Turn {turns}."},
    )

    await talk.say("first", "starts as another", "ends as another", "last")

    one, started, ended, last = await talk.turns()
    assert started["state"] == "uncertain" and f"started as session '{other}'" in started["reason"]
    assert "As another." not in talk.heard
    assert ended["state"] == "errored" and f"the result is of session '{other}'" in ended["reason"]
    assert ended["reply"] is None and ended["provider_session"] is None
    assert ended["provider_cost_usd"] == Decimal("0.002")  # what it used is kept
    # The conversation still continues the session it had: never the other one.
    launched = talk.launched()
    assert {each["session"] for each in launched} == {str(one["provider_session"])}
    assert last["state"] == "applied" and str(last["provider_session"]) != other


async def test_a_result_relay_has_read_is_recorded_though_the_host_is_interrupted(
    talk: Talk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3C-S4-1.3: the host is interrupted after the result was read and before it is
    written down. It is written down, once, with its reply and its charge."""
    import asyncio

    talk.script({"when": "first", "say": "One."})
    settle, reached, release = conversations.settle_turn, asyncio.Event(), asyncio.Event()

    async def held(*arguments: Any, **named: Any) -> bool:
        reached.set()
        await release.wait()
        return await settle(*arguments, **named)

    monkeypatch.setattr(conversations, "settle_turn", held)
    session = asyncio.create_task(talk.say("first"))
    await asyncio.wait_for(reached.wait(), 30)

    session.cancel()  # Ctrl-C, here
    await asyncio.sleep(0.2)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await session

    [turn] = await talk.turns()
    assert (turn["state"], turn["reply"]) == ("applied", "One.")
    assert turn["provider_cost_usd"] == Decimal("0.002")
    account = await live_usage(talk.world.database, PROJECT)
    assert account["by_role"]["interpreter"]["charged_by_the_provider"] == {
        "usd": "0.002",
        "calls_it_said_the_cost_of": 1,
        "calls_it_did_not": 0,
    }
    assert await talk.world.column("SELECT closed_at IS NOT NULL FROM conversations") == [True]


async def test_a_host_interrupted_while_its_call_runs_leaves_one_uncertain_call(
    talk: Talk,
) -> None:
    """3C-S4-1.3: and interrupted before any result: the call is ended and the turn is
    left as one nobody knows the end of, not as one still in flight."""
    import asyncio

    talk.script({"when": "first", "calls": [{"tool": "status"}] * 40, "say": "One."})
    session = asyncio.create_task(talk.say("first"))
    async with asyncio.timeout(30):
        while not talk.launched():
            await asyncio.sleep(0.05)

    session.cancel()
    with pytest.raises(asyncio.CancelledError):
        await session

    [turn] = await talk.turns()
    assert turn["state"] == "uncertain" and turn["ended_at"] is not None
    assert "interrupted" in turn["reason"] or "without a result" in turn["reason"]


async def test_a_new_provider_session_is_told_what_was_said_before(talk: Talk) -> None:
    """3C-S4-1.6: the first turn discusses two options and records nothing. The
    provider's transcript is lost. The follow-up is told both sides of the first turn."""
    talk.script(
        {"when": "options", "say": "Either a flat rate, or a rate for each country."},
        {"when": "the second option", "say": "A rate for each country costs more."},
    )

    def and_the_transcript_is_lost() -> str:
        for each in (talk.home / "sessions").iterdir():
            each.unlink()
        return "What would the second option cost?"

    await talk.say("What are my options for VAT?", and_the_transcript_is_lost)

    *_, afresh = talk.launched()
    assert not afresh["resumed"]
    told = afresh["told"]
    assert '"the_product_owner_said": "What are my options for VAT?"' in told
    assert '"the_interpreter_replied": "Either a flat rate, or a rate for each country."' in told
    assert told.endswith("What would the second option cost?")
    assert "A rate for each country costs more." in talk.heard


def test_the_server_is_told_where_the_installation_is_as_a_place_it_can_find(
    talk: Talk, tmp_path: Path
) -> None:
    """3C-S4-1.8: `RELAY_CONFIG=./relay.toml relay session`. The file is named relative to
    where the person is; the server is started somewhere else, and is given the file."""
    assert host.for_the_server(
        {"RELAY_CONFIG": "./relay.toml", "RELAY_PROJECT": "p", "HOME": "/h", "PATH": "/bin"},
        Path("/somewhere/else"),
    ) == {"RELAY_CONFIG": "/somewhere/else/relay.toml", "RELAY_PROJECT": "p"}
    absolute = str(tmp_path.resolve() / "relay.toml")
    assert host.for_the_server({"RELAY_CONFIG": absolute}, Path("/x")) == {"RELAY_CONFIG": absolute}
    assert host.for_the_server({"RELAY_PROJECT": "p"}, Path("/x")) == {"RELAY_PROJECT": "p"}
    # And through the command: everything the installation is found by is in the file.
    shell = dict(talk.desk.shell)
    (talk.desk.here / "relay.toml").write_text(
        "[relay]\n"
        f'database = "{shell.pop("RELAY_DATABASE_URL")}"\n'
        f'redis = "{shell.pop("RELAY_REDIS_URL")}"\n'
        f'schema = "{shell.pop("RELAY_DATABASE_SCHEMA")}"\n'
    )
    shell["RELAY_CONFIG"] = "./relay.toml"
    talk.script({"when": "where are we", "calls": [{"tool": "context"}], "say": "{results}"})

    ran = at_a_terminal(
        ["session", "--project", PROJECT], ["where are we?", "/quit"], shell, talk.desk.here
    )

    assert ran.code == 0, ran.printed
    assert f'"project": "{PROJECT}"' in ran.printed  # the server found the installation


async def test_starting_from_an_analysis_is_starting_from_that_version(talk: Talk) -> None:
    """3C-S4-1.9: two versions that say the same thing are still two. Which one the person
    adopted is shown to them, said to be a model's, and is part of the request."""
    world, desk = talk.world, talk.desk
    desk.shell["RELAY_PROJECT"] = PROJECT
    same = {"tool": "record_analysis", "arguments": {"text": "VAT is added twice on export."}}
    talk.script({"when": "VAT", "calls": [same, same], "say": "Recorded twice."})
    await talk.say("VAT is wrong")
    assert await world.column("SELECT version FROM analyses ORDER BY version") == [1, 2]

    started = desk.po("feature", "start", "--analysis", "1", "--request-id", "start-from-one")

    assert started.code == 0, started.printed
    assert "version 1 of the Interpreter's analysis, which a model wrote" in started.shown
    async with world.database.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT payload FROM human_actions WHERE request_id = 'start-from-one'"
            )
        ).fetchone()
    assert row is not None and row["payload"] == {
        "problem": "VAT is added twice on export.",
        "analysis": "1",
    }
    again = desk.po("feature", "start", "--analysis", "1", "--request-id", "start-from-one")
    other = desk.po("feature", "start", "--analysis", "2", "--request-id", "start-from-one")
    stated = desk.po(
        "feature", "start", "VAT is added twice on export.", "--request-id", "start-from-one"
    )
    assert again.code == 0 and "started F1 (as on record)" in again.printed
    for different in (other, stated):
        assert different.code == 1 and "a request for something else" in different.printed
    assert await world.column("SELECT count(*) FROM features") == [1]


async def test_with_no_statement_of_policy_no_session_is_launched(talk: Talk) -> None:
    """D-10."""
    async with talk.world.database.connection() as connection:
        await connection.execute("UPDATE installation SET policy = NULL")
    talk.script({"when": "hello", "say": "hello"})

    with pytest.raises(host.NotLaunched, match="relay policy state"):
        await talk.say("hello")

    assert talk.launched() == []
    assert await talk.world.column("SELECT count(*) FROM conversations") == [0]


async def test_a_cli_that_cannot_be_launched_leaves_a_turn_that_never_started(talk: Talk) -> None:
    talk.launch = host.Launch(**{**talk.launch.__dict__, "binary": str(talk.home / "no-such-cli")})

    await talk.say("hello")

    [turn] = await talk.turns()
    assert turn["state"] == "not_started" and "could not be launched" in turn["reason"]
    assert turn["gives_up_at"] is not None
    assert turn["said"] == "hello"
    account = await live_usage(talk.world.database, PROJECT)
    assert (account["calls"], account["calls_that_never_started"]) == (0, 1)


async def test_a_turn_left_open_by_a_host_that_died_is_found_by_the_next_session(
    talk: Talk,
) -> None:
    pool = talk.world.database
    old = await conversations.open_conversation(pool, PROJECT)
    await conversations.begin_turn(pool, old, "and then the terminal was closed", MODEL)
    async with pool.connection() as connection:
        await connection.execute(
            "UPDATE conversation_turns SET gives_up_at = gives_up_at - interval '1 hour'"
        )
    talk.script({"when": "hello", "say": "hello"})

    await talk.say("hello")

    left, new = await talk.turns()
    assert left["state"] == "uncertain" and new["state"] == "applied"


def test_relay_session_is_a_conversation_with_a_person_at_a_terminal(talk: Talk) -> None:
    """The command itself: the person's lines are read from their terminal, what the
    session says is printed, and a model that runs it with no terminal is refused."""
    talk.script(
        {"when": "where are we", "calls": [{"tool": "status"}], "say": "Nothing\x1b[2J yet."}
    )

    ran = at_a_terminal(
        ["session", "--project", PROJECT],
        ["where are we?", "/quit"],
        talk.desk.shell,
        talk.desk.here,
    )

    assert ran.code == 0, ran.printed
    assert "it decides nothing" in ran.shown and ran.shown.count("you: ") == 2
    assert "Nothing\\x1b[2J yet." in ran.printed and "\x1b[2J" not in ran.printed
    assert "0.002 USD charged by the provider" in ran.printed
    assert "ended after 1 turn(s)" in ran.printed
    [launched] = talk.launched()
    assert launched["told"].endswith("where are we?") and flag(launched["argv"], "--model") == MODEL
    as_a_model = with_no_terminal(
        ["session", "--project", PROJECT], talk.desk.shell, talk.desk.here, given="approve all\n"
    )
    assert as_a_model.code == 1 and "needs a person at a terminal" in as_a_model.printed
    assert len(talk.launched()) == 1  # and nothing was launched for it
    assert uuid.UUID(launched["session"])
    # The person ends the input (Ctrl-D): the conversation ends, and is not asked again.
    ended = at_a_terminal(
        ["session", "--project", PROJECT],
        ["where are we?", "\x04"],
        talk.desk.shell,
        talk.desk.here,
    )
    assert ended.code == 0 and "ended after 1 turn(s)" in ended.printed


async def test_a_feature_is_started_from_an_analysis_only_by_the_person_naming_it(
    talk: Talk,
) -> None:
    """3C-2.9: an analysis becomes a feature only when the Product Owner starts one, and
    they may name the version it starts from. The words are shown before the passphrase."""
    world, desk = talk.world, talk.desk
    desk.shell["RELAY_PROJECT"] = PROJECT
    talk.script(
        {
            "when": "VAT",
            "calls": [
                {"tool": "record_analysis", "arguments": {"text": "VAT is added twice on export."}}
            ],
            "say": "Recorded.",
        }
    )
    await talk.say("VAT is wrong")
    assert await world.column("SELECT count(*) FROM features") == [0]  # it started nothing

    neither = desk.po("feature", "start")
    both = desk.po("feature", "start", "Another problem", "--analysis", "1")
    missing = desk.po("feature", "start", "--analysis", "7")
    started = desk.po("feature", "start", "--analysis", "1")

    for refused in (neither, both):
        assert refused.code == 1 and "one of them, and not both" in refused.printed
    assert missing.code == 1 and "there is no analysis 7" in missing.printed
    assert started.code == 0, started.printed
    assert "VAT is added twice on export." in started.shown  # what they are starting
    assert await world.column("SELECT problem FROM features") == ["VAT is added twice on export."]


@pytest.mark.skipif(
    os.environ.get("RELAY_REAL_CLAUDE") != "1", reason="makes paid calls to the real CLI"
)
async def test_the_real_cli_holds_a_conversation_through_relays_server(talk: Talk) -> None:
    """Opt-in, paid, on the cheapest model. What step 4 rests on, against the real CLI:
    print mode starts with exactly Relay's server and its tools, calls them, and continues
    the session on the next call."""
    world = talk.world
    gate = await world.roadmap(STANDARD_RATE)
    talk.launch = host.Launch(
        **{**talk.launch.__dict__, "binary": "claude", "environment": dict(os.environ)}
    )

    await talk.say(
        "Call the context tool. Then call record_analysis with the text 'HERON-4471'. "
        "Then tell me in one sentence how many items wait for the Product Owner.",
        "Without calling any tool: what text did I ask you to record? Reply with it alone.",
    )

    one, two = await talk.turns()
    assert (one["state"], two["state"]) == ("applied", "applied"), (one["reason"], two["reason"])
    assert (one["continued"], two["continued"]) == (False, True)
    assert one["provider_session"] == two["provider_session"]
    assert await world.column("SELECT text FROM analyses") == ["HERON-4471"]  # through the server
    assert "HERON-4471" in two["reply"]  # and the second call remembered the first
    assert one["provider_cost_usd"] is not None and one["usage"]
    # And the promised history, through the real session: an item read from the record,
    # its approval prepared by the model, and the decision made by the person alone.
    await talk.say(
        "Call the inbox tool. There is one item, a roadmap. Call the prepare tool to "
        "propose approving it: action 'approve', of the item's handle, why 'it is what was "
        "asked for'. Then tell me the code."
    )
    [code] = await world.column("SELECT code FROM prepared_actions")
    assert await world.column("SELECT state FROM gates") == ["open"]  # the model decided nothing
    assert await world.column("SELECT target, prepared_by FROM prepared_actions") == [str(gate)]
    assert str(code) in "\n".join(talk.heard)
    confirmed = talk.desk.po("confirm", str(code))
    assert confirmed.code == 0, confirmed.printed
    assert await world.column("SELECT outcome FROM gates") == ["approved"]
    print("\n".join(talk.heard))
