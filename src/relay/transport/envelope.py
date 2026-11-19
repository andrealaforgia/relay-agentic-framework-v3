"""The versioned message envelope (specification section 10)."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = 1


class MessageKind(StrEnum):
    COMMAND = "command"
    EVENT = "event"
    AGENT_RESPONSE = "agent_response"


@dataclass(frozen=True)
class Envelope:
    message_kind: MessageKind
    event_type: str
    project_id: str
    sender: str
    recipient: str
    idempotency_key: str
    correlation_id: uuid.UUID
    causation_id: uuid.UUID | None = None
    run_id: str | None = None
    story_id: str | None = None
    behaviour_id: str | None = None
    conversation_id: str | None = None
    artefact_ref: str | None = None
    attempt: int = 1
    payload: dict[str, Any] = field(default_factory=dict)
    message_id: uuid.UUID = field(default_factory=uuid.uuid7)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        for key in ("message_id", "correlation_id", "causation_id"):
            if document[key] is not None:
                document[key] = str(document[key])
        document["message_kind"] = self.message_kind.value
        document["timestamp"] = self.timestamp.isoformat()
        return document
