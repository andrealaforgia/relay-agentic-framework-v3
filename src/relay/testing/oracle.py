"""The process oracle: trusted code that launches the subject and records what it observed."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path
from typing import Protocol

from relay.domain.evidence import Ineligibility, Launch, ProcessObservation

# A subject returning one of these cannot be told apart from a launcher that failed to
# start it, so the run is recorded as not executed rather than guessed at.
_LAUNCHER_STATUSES = frozenset({125, 126, 127})
_DEFAULT_CAPTURE_LIMIT_BYTES = 4 * 1024 * 1024


class Isolation(Protocol):
    """How the subject is kept away from the supervisor that observes it."""

    name: str

    def command(self, workdir: Path, argv: tuple[str, ...]) -> tuple[str, ...]: ...

    def environment(self, workdir: Path) -> dict[str, str]: ...


class NativeIsolation:
    """Runs the subject as a child process with a scrubbed environment. It shares the
    supervisor's account and filesystem, so it is for development, not for hostile code."""

    name = "native"

    def command(self, workdir: Path, argv: tuple[str, ...]) -> tuple[str, ...]:
        return argv

    def environment(self, workdir: Path) -> dict[str, str]:
        return {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(workdir), "LANG": "C.UTF-8"}


class ProcessOracle:
    def __init__(
        self, isolation: Isolation, capture_limit_bytes: int = _DEFAULT_CAPTURE_LIMIT_BYTES
    ) -> None:
        self.isolation = isolation
        self._capture_limit = capture_limit_bytes

    async def observe(self, workdir: Path, launch: Launch) -> ProcessObservation:
        try:
            process = await asyncio.create_subprocess_exec(
                *self.isolation.command(workdir, launch.argv),
                cwd=workdir,
                env=self.isolation.environment(workdir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            return ProcessObservation(
                stderr=str(error).encode(), ineligibility=Ineligibility.NOT_EXECUTED
            )
        assert process.stdin and process.stdout and process.stderr
        capture = asyncio.gather(
            _feed(process.stdin, launch.stdin.encode()),
            _read_up_to(process.stdout, self._capture_limit),
            _read_up_to(process.stderr, self._capture_limit),
            process.wait(),
        )
        try:
            (
                _,
                (stdout, stdout_overflowed),
                (stderr, stderr_overflowed),
                exit_code,
            ) = await asyncio.wait_for(capture, timeout=launch.timeout_seconds)
        except TimeoutError:
            await _stop(process)
            return ProcessObservation(ineligibility=Ineligibility.TIMED_OUT)
        return ProcessObservation(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            ineligibility=_ineligibility(exit_code, stdout_overflowed or stderr_overflowed),
        )


def _ineligibility(exit_code: int, overflowed: bool) -> Ineligibility | None:
    if overflowed:
        return Ineligibility.OUTPUT_OVERFLOW
    if exit_code < 0:
        return Ineligibility.KILLED_BY_SIGNAL
    if exit_code in _LAUNCHER_STATUSES:
        return Ineligibility.NOT_EXECUTED
    return None


async def _feed(stdin: asyncio.StreamWriter, data: bytes) -> None:
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        stdin.write(data)
        await stdin.drain()
        stdin.close()
        await stdin.wait_closed()


async def _read_up_to(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    """Read the whole stream, keeping at most `limit` bytes and saying whether more arrived.
    The rest is drained and dropped so the subject is never blocked on a full pipe."""
    kept = bytearray()
    overflowed = False
    while chunk := await stream.read(65536):
        room = limit - len(kept)
        kept += chunk[:room]
        overflowed = overflowed or len(chunk) > room
    return bytes(kept), overflowed


async def _stop(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()
