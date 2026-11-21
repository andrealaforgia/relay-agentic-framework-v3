"""The Claude adapter, against a stand-in for the CLI that replays recorded streams.
What it must get right is what kind of ending a call had, because that decides whether
a paid call may ever be made again without asking a person."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from relay.providers.claude import (
    ClaudeAdapter,
    Confinement,
    Outcome,
    ProviderUnavailable,
    Request,
)
from relay.providers.definitions import AgentDefinition

RECORDED = Path(__file__).parent.parent / "fixtures" / "claude"
STUB = str(RECORDED / "stub_claude.py")
SCHEMA = {
    "type": "object",
    "properties": {"sum": {"type": "integer"}},
    "required": ["sum"],
    "additionalProperties": False,
}
DEFINITION = AgentDefinition(
    name="alf-test-design-reviewer",
    path=Path("/agents/alf-test-design-reviewer.md"),
    sha256="0" * 64,
    revision=None,
    dirty=False,
    description="Evaluates test code quality.",
    prompt="You review tests.\n",
)


def request(tmp_path: Path, **changes: Any) -> Request:
    given: dict[str, Any] = {
        "invocation_id": uuid.UUID("01a1fe00-0000-7000-8000-000000000001"),
        "definition": DEFINITION,
        "model": "claude-haiku-5-5",
        "briefing": "Add 2 and 3. The capability is not in here.",
        "schema": SCHEMA,
        "workdir": tmp_path,
        "confinement": Confinement(tools=(), settings={"sandbox": {"enabled": True}}),
        "timeout": 30.0,
    }
    return Request(**{**given, **changes})


def adapter(
    tmp_path: Path, stream: str | None = "success.jsonl", **environment: str
) -> ClaudeAdapter:
    return ClaudeAdapter(
        STUB,
        {
            "PATH": os.environ["PATH"],
            "STUB_RECORD": str(tmp_path / "call.json"),
            **({"STUB_STREAM": str(RECORDED / stream)} if stream else {}),
            **environment,
        },
    )


def called(tmp_path: Path) -> dict[str, Any]:
    recorded: dict[str, Any] = json.loads((tmp_path / "call.json").read_text())
    return recorded


def following(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


async def test_a_call_that_succeeded_is_returned_with_its_terminal_event(tmp_path: Path) -> None:
    result = await adapter(tmp_path).invoke(request(tmp_path))

    assert result.outcome is Outcome.RETURNED
    assert result.terminal is not None
    assert result.terminal.structured_output == {"sum": 5}
    assert result.terminal.provider_cost_usd == Decimal("0.0008912")


async def test_the_briefing_goes_in_on_standard_input_and_never_on_the_command_line(
    tmp_path: Path,
) -> None:
    await adapter(tmp_path).invoke(request(tmp_path))

    call = called(tmp_path)
    assert call["stdin"] == "Add 2 and 3. The capability is not in here."
    assert not any("capability" in argument for argument in call["argv"])
    assert Path(call["cwd"]).resolve() == tmp_path.resolve()


async def test_the_run_is_asked_for_exactly_what_was_resolved(tmp_path: Path) -> None:
    await adapter(tmp_path).invoke(
        request(
            tmp_path,
            effort="high",
            budget_usd=Decimal("1.50"),
            confinement=Confinement(
                tools=("Read", "Grep"), settings={"sandbox": {"enabled": True}}
            ),
        )
    )

    argv = called(tmp_path)["argv"]
    assert argv[:4] == ["-p", "--output-format", "stream-json", "--verbose"]
    assert following(argv, "--model") == "claude-haiku-5-5"
    assert following(argv, "--effort") == "high"
    assert following(argv, "--max-budget-usd") == "1.50"
    assert following(argv, "--session-id") == "01a1fe00-0000-7000-8000-000000000001"
    assert following(argv, "--tools") == "Read,Grep"
    assert json.loads(following(argv, "--json-schema")) == SCHEMA
    assert json.loads(following(argv, "--settings")) == {"sandbox": {"enabled": True}}
    assert following(argv, "--setting-sources") == ""
    assert following(argv, "--permission-prompts") == "none"
    for alone in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert alone in argv


async def test_the_definition_that_runs_is_the_content_that_was_recorded(tmp_path: Path) -> None:
    """Passed inline, with the role's tools in place of whatever the file asked for."""
    await adapter(tmp_path).invoke(
        request(tmp_path, confinement=Confinement(tools=("Read",), settings={}))
    )

    argv = called(tmp_path)["argv"]
    assert following(argv, "--agent") == "alf-test-design-reviewer"
    assert json.loads(following(argv, "--agents")) == {
        "alf-test-design-reviewer": {
            "description": "Evaluates test code quality.",
            "prompt": "You review tests.\n",
            "tools": ["Read"],
        }
    }


async def test_no_effort_and_no_budget_are_passed_unless_asked_for(tmp_path: Path) -> None:
    await adapter(tmp_path).invoke(request(tmp_path))

    argv = called(tmp_path)["argv"]
    assert "--effort" not in argv and "--max-budget-usd" not in argv


async def test_the_run_gets_the_environment_it_was_given_and_nothing_of_the_supervisors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RELAY_DATABASE_URL", "postgresql://relay:secret@db/relay")

    await adapter(tmp_path).invoke(request(tmp_path))

    assert "RELAY_DATABASE_URL" not in called(tmp_path)["env"]


async def test_an_adapter_given_no_environment_passes_on_only_what_a_run_needs_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    monkeypatch.setenv("STUB_RECORD", str(tmp_path / "call.json"))  # not passed on either

    result = await ClaudeAdapter(STUB).invoke(request(tmp_path))

    assert result.outcome is Outcome.UNCERTAIN  # the stand-in was told nothing to replay
    assert not (tmp_path / "call.json").exists()


async def test_a_run_that_ends_in_an_error_is_known_and_is_not_a_reply(tmp_path: Path) -> None:
    result = await adapter(tmp_path, "budget_exhausted.jsonl", STUB_EXIT="1").invoke(
        request(tmp_path)
    )

    assert result.outcome is Outcome.ERRORED
    assert result.terminal is not None and result.terminal.subtype == "error_max_budget_usd"
    assert result.terminal.usage  # what it used is still on record


async def test_a_stream_that_ends_without_a_terminal_event_is_uncertain(tmp_path: Path) -> None:
    result = await adapter(tmp_path, "cut_off.jsonl", STUB_STDERR="socket hang up").invoke(
        request(tmp_path)
    )

    assert result.outcome is Outcome.UNCERTAIN
    assert result.terminal is None
    assert "socket hang up" in result.reason


async def test_a_run_that_does_not_finish_is_killed_with_its_children_and_is_uncertain(
    tmp_path: Path,
) -> None:
    result = await adapter(tmp_path, "cut_off.jsonl", STUB_HANG="1").invoke(
        request(tmp_path, timeout=1.0)
    )

    assert result.outcome is Outcome.UNCERTAIN
    assert "1.0" in result.reason
    assert result.process_group is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(result.process_group, signal.SIGCONT)  # nobody is left in it


async def test_a_terminal_event_is_the_result_even_if_the_process_then_lingers(
    tmp_path: Path,
) -> None:
    result = await adapter(tmp_path, "success.jsonl", STUB_HANG="1").invoke(
        request(tmp_path, timeout=30.0)
    )

    assert result.outcome is Outcome.RETURNED
    assert result.process_group is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(result.process_group, signal.SIGCONT)


async def test_a_binary_that_cannot_be_launched_was_not_started(tmp_path: Path) -> None:
    missing = ClaudeAdapter(str(tmp_path / "no-such-claude"), {"PATH": os.environ["PATH"]})

    result = await missing.invoke(request(tmp_path), checked=False)

    assert result.outcome is Outcome.NOT_STARTED
    assert result.terminal is None


@pytest.mark.parametrize(
    ("tools", "servers", "named"),
    [
        (["StructuredOutput", "Bash"], [], "Bash"),
        (["StructuredOutput"], [{"name": "mail", "status": "connected"}], "mail"),
    ],
)
async def test_a_run_that_has_a_tool_or_a_server_it_was_not_given_is_ended(
    tmp_path: Path, tools: list[str], servers: list[dict[str, str]], named: str
) -> None:
    """3AII-S3-20.5: the start event says what the run actually has."""
    unconfined = tmp_path / "unconfined.jsonl"
    started = json.loads((RECORDED / "success.jsonl").read_text().splitlines()[0])
    started["tools"], started["mcp_servers"] = tools, servers
    unconfined.write_text(json.dumps(started) + "\n")

    result = await ClaudeAdapter(
        STUB, {"PATH": os.environ["PATH"], "STUB_STREAM": str(unconfined), "STUB_HANG": "1"}
    ).invoke(request(tmp_path))

    assert result.outcome is Outcome.UNCERTAIN
    assert "not confined" in result.reason and named in result.reason


async def test_the_tools_a_run_was_given_are_not_a_breach(tmp_path: Path) -> None:
    result = await adapter(tmp_path).invoke(
        request(tmp_path, confinement=Confinement(tools=("Read",), settings={}))
    )

    assert result.outcome is Outcome.RETURNED  # it lists fewer than it was allowed


async def test_capabilities_are_what_the_installed_cli_says(tmp_path: Path) -> None:
    found = await adapter(tmp_path).capabilities()

    assert found.version == "9.9.9 (stub)"
    assert "--json-schema" in found.flags and "--restricted" in found.flags


async def test_a_cli_without_a_flag_relay_passes_is_refused_before_any_call(tmp_path: Path) -> None:
    old = tmp_path / "help.txt"
    old.write_text("  -p, --print  --output-format <format>  --model <model>\n")

    with pytest.raises(ProviderUnavailable, match="--json-schema"):
        await adapter(tmp_path, STUB_HELP=str(old)).invoke(request(tmp_path))

    assert not (tmp_path / "call.json").exists()


async def test_cancelling_the_caller_kills_the_run(tmp_path: Path) -> None:
    held = adapter(tmp_path, "cut_off.jsonl", STUB_HANG="1")
    groups: list[int] = []
    calling = asyncio.create_task(held.invoke(request(tmp_path), on_start=groups.append))
    async with asyncio.timeout(20):
        while not groups:
            await asyncio.sleep(0.05)

    calling.cancel()
    with pytest.raises(asyncio.CancelledError):
        await calling

    with pytest.raises(ProcessLookupError):
        os.killpg(groups[0], signal.SIGCONT)


async def test_an_effort_the_cli_cannot_be_asked_for_is_refused_and_not_dropped(
    tmp_path: Path,
) -> None:
    """S3-18.5: what is recorded as asked for is what was asked for."""
    without = tmp_path / "help.txt"
    without.write_text(
        "  -p, --print --output-format --verbose --agents --agent --model --json-schema\n"
        "  --session-id --tools --settings --setting-sources --strict-mcp-config\n"
        "  --disable-slash-commands --permission-prompts --no-session-persistence\n"
    )
    limited = adapter(tmp_path, STUB_HELP=str(without))

    assert (await limited.invoke(request(tmp_path))).outcome is Outcome.RETURNED
    with pytest.raises(ProviderUnavailable, match="--effort"):
        await limited.invoke(request(tmp_path, effort="high"))
    with pytest.raises(ProviderUnavailable, match="--max-budget-usd"):
        await limited.invoke(request(tmp_path, budget_usd=Decimal(1)))


@pytest.mark.parametrize("missing", ["tools", "mcp_servers"])
async def test_a_run_that_does_not_say_what_it_has_is_ended(tmp_path: Path, missing: str) -> None:
    """3B-S1-1.9: an inventory that is missing is not an empty one."""
    silent = tmp_path / "silent.jsonl"
    started = json.loads((RECORDED / "success.jsonl").read_text().splitlines()[0])
    del started[missing]
    silent.write_text(json.dumps(started) + "\n")

    result = await ClaudeAdapter(
        STUB, {"PATH": os.environ["PATH"], "STUB_STREAM": str(silent), "STUB_HANG": "1"}
    ).invoke(request(tmp_path))

    assert result.outcome is Outcome.UNCERTAIN
    assert "did not say" in result.reason


async def test_a_run_that_is_not_the_session_relay_started_is_ended(tmp_path: Path) -> None:
    result = await adapter(tmp_path, STUB_KEEP_SESSION="1", STUB_HANG="1").invoke(
        request(tmp_path, invocation_id=uuid.uuid7())
    )

    assert result.outcome is Outcome.UNCERTAIN
    assert "not the session" in result.reason


async def test_a_result_from_a_run_that_never_said_what_it_had_is_not_a_reply(
    tmp_path: Path,
) -> None:
    """3B-S1-1.9: it is kept, with what it used, and it cannot be used."""
    headless = tmp_path / "headless.jsonl"
    headless.write_text((RECORDED / "success.jsonl").read_text().split("\n", 1)[1])

    result = await ClaudeAdapter(
        STUB, {"PATH": os.environ["PATH"], "STUB_STREAM": str(headless)}
    ).invoke(request(tmp_path))

    assert result.outcome is Outcome.RETURNED
    assert result.terminal is not None and result.terminal.usage
    assert "without first saying what it had" in result.unusable


async def test_the_result_is_told_as_classified_before_the_process_is_waited_for(
    tmp_path: Path,
) -> None:
    """3B-S1-2.2: a caller cut short while the run lingers has what the adapter decided."""
    headless = tmp_path / "headless.jsonl"
    headless.write_text((RECORDED / "success.jsonl").read_text().split("\n", 1)[1])
    told: list[Any] = []

    result = await ClaudeAdapter(
        STUB, {"PATH": os.environ["PATH"], "STUB_STREAM": str(headless)}
    ).invoke(request(tmp_path), on_result=told.append)

    assert told == [result]
    assert told[0].unusable
