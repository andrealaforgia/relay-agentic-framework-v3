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

import fcntl
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
    file.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(file, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        # One command at a time, from looking for the request to writing it: two that
        # use one identity for the first time at once are one after the other, and the
        # second finds what the first wrote.
        fcntl.flock(descriptor, fcntl.LOCK_EX)
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
        line = (json.dumps(asdict(request), sort_keys=True) + "\n").encode()
        size = os.fstat(descriptor).st_size
        if size and os.pread(descriptor, 1, size - 1) != b"\n":
            # A record cut short by a crash. It is ended here, as a line that is no
            # request, so that this record is not joined to it and lost with it.
            line = b"\n" + line
        written = 0
        while written < len(line):
            written += os.write(descriptor, line[written:])
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
