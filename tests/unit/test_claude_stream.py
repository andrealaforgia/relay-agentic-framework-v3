"""Reading what the Claude CLI prints, from streams recorded from real runs. The
terminal event is kept whole and classified by its own fields; nothing is inferred."""

import json
from decimal import Decimal
from pathlib import Path

from relay.providers.stream import Started, Terminal, ToolCall, ToolOutcome, read_stream

RECORDED = Path(__file__).parent.parent / "fixtures" / "claude"


def events(name: str) -> list[object]:
    return read_stream((RECORDED / name).read_text().splitlines())


def terminal(name: str) -> Terminal:
    [found] = [each for each in events(name) if isinstance(each, Terminal)]
    return found


def test_a_run_that_succeeded_carries_its_structured_reply_apart_from_its_prose() -> None:
    ended = terminal("success.jsonl")

    assert ended.succeeded
    assert ended.structured_output == {"sum": 5}
    assert ended.text == '{"sum":5}'
    assert (ended.subtype, ended.terminal_reason) == ("success", "completed")
    assert ended.session_id == "01a1fe00-0000-7000-8000-000000000001"
    assert ended.envelope["num_turns"] == 2  # the whole event is kept as it arrived


def test_usage_is_what_the_provider_reported_by_the_model_it_billed() -> None:
    [billed] = terminal("success.jsonl").usage

    assert billed.model == "claude-haiku-5-5"
    assert (billed.input_tokens, billed.output_tokens) == (2, 52)
    assert (billed.cache_read_tokens, billed.cache_write_tokens) == (0, 4325)
    assert (billed.cache_write_5m_tokens, billed.cache_write_1h_tokens) == (0, 4325)
    assert billed.provider_cost_usd == Decimal("0.0008912")
    assert terminal("success.jsonl").provider_cost_usd == Decimal("0.0008912")


def test_a_run_that_ended_in_an_error_is_known_and_still_reports_what_it_used() -> None:
    ended = terminal("budget_exhausted.jsonl")

    assert not ended.succeeded
    assert (ended.subtype, ended.terminal_reason) == ("error_max_budget_usd", "budget_exhausted")
    assert ended.structured_output is None
    [billed] = ended.usage
    assert (billed.cache_read_tokens, billed.cache_write_1h_tokens) == (1080, 3229)
    assert billed.provider_cost_usd is not None


def test_a_stream_cut_off_before_its_end_has_no_terminal_event() -> None:
    assert [each for each in events("cut_off.jsonl") if isinstance(each, Terminal)] == []


def test_the_start_says_which_tools_and_servers_the_run_actually_has() -> None:
    [started] = [each for each in events("success.jsonl") if isinstance(each, Started)]

    assert started.model == "claude-haiku-5-5"
    assert started.tools == ("StructuredOutput",)
    assert started.mcp_servers == ()
    assert started.version


def test_tool_calls_and_their_outcomes_are_read_from_the_stream_itself() -> None:
    """What the model did, not what it says it did."""
    [call] = [each for each in events("success.jsonl") if isinstance(each, ToolCall)]
    [outcome] = [each for each in events("success.jsonl") if isinstance(each, ToolOutcome)]

    assert (call.name, call.input) == ("StructuredOutput", {"sum": 5})
    assert outcome.tool_use_id == call.tool_use_id
    assert not outcome.is_error


def test_lines_that_are_not_events_are_passed_over() -> None:
    assert (
        read_stream(["", "not json", "[1, 2]", '{"type": "something_new"}', '{"no": "type"}']) == []
    )


def test_counters_the_provider_did_not_report_are_not_zero() -> None:
    [ended] = read_stream(
        [
            '{"type": "result", "subtype": "success", "is_error": false, "usage": {"input_tokens": 7}}'
        ]
    )

    assert isinstance(ended, Terminal)
    [reported] = ended.usage
    assert reported.model is None
    assert reported.input_tokens == 7
    assert reported.output_tokens is None
    assert reported.cache_write_1h_tokens is None
    assert ended.provider_cost_usd is None


def test_a_terminal_event_is_a_success_only_if_both_of_its_fields_say_so() -> None:
    def ended(**fields: object) -> Terminal:
        [found] = read_stream([json.dumps({"type": "result", **fields})])
        assert isinstance(found, Terminal)
        return found

    assert ended(subtype="success", is_error=False).succeeded
    assert not ended(subtype="success", is_error=True).succeeded
    assert not ended(subtype="error_during_execution", is_error=False).succeeded
    assert not ended(subtype="success").succeeded


def test_a_tool_call_that_was_refused_is_read_as_refused() -> None:
    [outcome] = read_stream(
        [
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_1",
                                "is_error": True,
                                "content": "Permission to read /etc/passwd was denied",
                            }
                        ]
                    },
                }
            )
        ]
    )

    assert isinstance(outcome, ToolOutcome)
    assert outcome.is_error and "denied" in outcome.content
