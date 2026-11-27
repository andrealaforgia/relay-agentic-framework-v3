"""The journal of requests: the handles to what a person asked Relay to do.

Every command that acts makes its request's identity before it does anything, writes it
here with the command line, and prints it. Sent again under that identity, a request
returns the receipt it was given and does nothing more. The journal records which
installation and project the request was resolved to, and a replay uses those: not the
directory it happens to be replayed from.

It is a file of this user's, on this machine. The receipts are in the database; the
handles are here.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class Request:
    request_id: str
    at: str
    command: tuple[str, ...]
    installation: str
    project: str | None


def journal_file(environ: Mapping[str, str] = os.environ) -> Path:
    if environ.get("RELAY_STATE_DIR"):
        return Path(environ["RELAY_STATE_DIR"]) / "requests.jsonl"
    state = environ.get("XDG_STATE_HOME") or str(Path(environ.get("HOME", "~")) / ".local/state")
    return Path(state) / "relay" / "requests.jsonl"


def note(
    file: Path,
    command: Sequence[str],
    installation: str,
    project: str | None,
    request_id: str | None = None,
) -> Request:
    """Write a request down before anything is done for it. Given the identity of one
    already written, returns that one as it was written, and writes nothing."""
    if request_id is not None:
        for known in read(file):
            if known.request_id == request_id:
                return known
    request = Request(
        request_id=request_id or str(uuid.uuid7()),
        at=datetime.now(UTC).isoformat(timespec="seconds"),
        command=tuple(command),
        installation=installation,
        project=project,
    )
    file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(request), sort_keys=True) + "\n"
    # One write of one line, appended: two commands at once each leave a whole line.
    descriptor = os.open(file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, line.encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return request


def read(file: Path) -> list[Request]:
    if not file.is_file():
        return []
    requests = []
    for line in file.read_text().splitlines():
        try:
            said = json.loads(line)
            requests.append(
                Request(
                    str(said["request_id"]),
                    str(said["at"]),
                    tuple(said["command"]),
                    str(said["installation"]),
                    said.get("project"),
                )
            )
        except ValueError, KeyError, TypeError:
            continue  # a line cut short by a crash is no request
    return requests
