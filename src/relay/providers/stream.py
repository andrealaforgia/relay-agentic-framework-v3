"""Reading the Claude CLI's `stream-json` output: one JSON event per line.

Only what Relay needs is given a shape: the start (what the run actually has), the tool
calls the model made and what came back, and the terminal event. The terminal event is
kept whole, because it is the record of a paid call, and is classified by its own
fields. A line that is not an event Relay knows is passed over: the format grows.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Started:
    session_id: str
    model: str
    # What the run says it has. None where it did not say: an inventory that is missing
    # is not an empty one.
    tools: tuple[str, ...] | None
    mcp_servers: tuple[str, ...] | None
    version: str


@dataclass(frozen=True)
class ToolCall:
    tool_use_id: str
    name: str
    input: Any


@dataclass(frozen=True)
class ToolOutcome:
    tool_use_id: str
    is_error: bool
    content: str


@dataclass(frozen=True)
class Said:
    text: str


@dataclass(frozen=True)
class ModelUsage:
    """What one model was reported to have used. A counter the provider did not report
    is None, never zero: zero is a claim."""

    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cache_write_5m_tokens: int | None
    cache_write_1h_tokens: int | None
    provider_cost_usd: Decimal | None


@dataclass(frozen=True)
class Terminal:
    """The event that ends a run. `envelope` is the event as it arrived."""

    envelope: dict[str, Any]

    @property
    def succeeded(self) -> bool:
        return self.envelope.get("subtype") == "success" and self.envelope.get("is_error") is False

    @property
    def subtype(self) -> str | None:
        return _text(self.envelope.get("subtype"))

    @property
    def terminal_reason(self) -> str | None:
        return _text(self.envelope.get("terminal_reason"))

    @property
    def session_id(self) -> str | None:
        return _text(self.envelope.get("session_id"))

    @property
    def text(self) -> str | None:
        return _text(self.envelope.get("result"))

    @property
    def structured_output(self) -> Any:
        """The reply that satisfied the schema the run was given, if there is one."""
        return self.envelope.get("structured_output")

    @property
    def provider_cost_usd(self) -> Decimal | None:
        return _money(self.envelope.get("total_cost_usd"))

    @property
    def usage(self) -> tuple[ModelUsage, ...]:
        by_model = self.envelope.get("modelUsage")
        if isinstance(by_model, dict) and by_model:
            return tuple(
                ModelUsage(
                    model=model,
                    input_tokens=_count(used.get("inputTokens")),
                    output_tokens=_count(used.get("outputTokens")),
                    cache_read_tokens=_count(used.get("cacheReadInputTokens")),
                    cache_write_tokens=_count(used.get("cacheCreationInputTokens")),
                    cache_write_5m_tokens=_count(used.get("cacheCreationEphemeral5mInputTokens")),
                    cache_write_1h_tokens=_count(used.get("cacheCreationEphemeral1hInputTokens")),
                    provider_cost_usd=_money(used.get("costUSD")),
                )
                for model, used in by_model.items()
                if isinstance(used, dict)
            )
        used = self.envelope.get("usage")
        if not isinstance(used, dict):
            return ()
        by_lifetime = used.get("cache_creation")
        by_lifetime = by_lifetime if isinstance(by_lifetime, dict) else {}
        # Without the breakdown by model, nothing says which model this was.
        return (
            ModelUsage(
                model=None,
                input_tokens=_count(used.get("input_tokens")),
                output_tokens=_count(used.get("output_tokens")),
                cache_read_tokens=_count(used.get("cache_read_input_tokens")),
                cache_write_tokens=_count(used.get("cache_creation_input_tokens")),
                cache_write_5m_tokens=_count(by_lifetime.get("ephemeral_5m_input_tokens")),
                cache_write_1h_tokens=_count(by_lifetime.get("ephemeral_1h_input_tokens")),
                provider_cost_usd=None,
            ),
        )


Event = Started | ToolCall | ToolOutcome | Said | Terminal


def read_stream(lines: Iterable[str]) -> list[Event]:
    return [event for line in lines for event in read_line(line)]


def read_line(line: str) -> list[Event]:
    try:
        event = json.loads(line)
    except ValueError:
        return []
    if not isinstance(event, dict):
        return []
    kind = event.get("type")
    if kind == "result":
        return [Terminal(event)]
    if kind == "system" and event.get("subtype") == "init":
        return [
            Started(
                session_id=str(event.get("session_id", "")),
                model=str(event.get("model", "")),
                tools=_names(event.get("tools")),
                mcp_servers=_names(event.get("mcp_servers")),
                version=str(event.get("claude_code_version", "")),
            )
        ]
    if kind in ("assistant", "user"):
        return [found for block in _blocks(event) for found in _read_block(block)]
    return []


def _blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return (
        [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
    )


def _read_block(block: dict[str, Any]) -> list[Event]:
    kind = block.get("type")
    if kind == "tool_use":
        return [ToolCall(str(block.get("id", "")), str(block.get("name", "")), block.get("input"))]
    if kind == "tool_result":
        content = block.get("content")
        return [
            ToolOutcome(
                str(block.get("tool_use_id", "")),
                block.get("is_error") is True,
                content if isinstance(content, str) else json.dumps(content),
            )
        ]
    if kind == "text" and isinstance(block.get("text"), str):
        return [Said(block["text"])]
    return []


def _names(listed: Any) -> tuple[str, ...] | None:
    """Tools are listed by name; servers as objects with a name."""
    if not isinstance(listed, list):
        return None
    return tuple(
        str(each.get("name", "")) if isinstance(each, dict) else str(each) for each in listed
    )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _money(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # The shortest decimal that reads back as the number reported: 0.0008912, not its
    # binary neighbour.
    return Decimal(repr(value))
