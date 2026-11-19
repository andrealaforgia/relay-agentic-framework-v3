"""The process oracle: trusted code that launches the subject and records what it observed."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from relay.domain.evidence import Ineligibility, Launch, ProcessObservation

# A subject returning one of these cannot be told apart from a launcher that failed to
# start it, so the run is recorded as not executed rather than guessed at.
_LAUNCHER_STATUSES = frozenset({125, 126, 127})
_DEFAULT_CAPTURE_LIMIT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class PreparedLaunch:
    command: tuple[str, ...]
    environment: dict[str, str]
    stop_command: tuple[str, ...] | None = None


class Isolation(Protocol):
    """How the subject is kept away from the supervisor that observes it."""

    name: str

    def prepare(self, workdir: Path, argv: tuple[str, ...]) -> PreparedLaunch: ...


class NativeIsolation:
    """Runs the subject as a child process with a scrubbed environment. It shares the
    supervisor's account and filesystem, so it is for development, not for hostile code."""

    name = "native"

    def prepare(self, workdir: Path, argv: tuple[str, ...]) -> PreparedLaunch:
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(workdir),
            "LANG": "C.UTF-8",
        }
        return PreparedLaunch(argv, environment)


class ContainerIsolation:
    """Runs the subject in a disposable container: no network, read-only root and checkout,
    no capabilities, bounded memory, processes and CPU, and none of the host's environment."""

    name = "container"

    def __init__(self, image: str) -> None:
        if "@sha256:" not in image:
            raise ValueError(f"the runner image must be pinned by digest, not {image!r}")
        self.image = image

    def prepare(self, workdir: Path, argv: tuple[str, ...]) -> PreparedLaunch:
        container = f"relay-oracle-{uuid.uuid4().hex}"
        command = (
            "docker", "run", "--rm", "--interactive", "--name", container,
            "--network", "none",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "256",
            "--memory", "512m",
            "--cpus", "1",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--volume", f"{workdir}:/subject:ro",
            "--workdir", "/subject",
            self.image,
            *argv,
        )  # fmt: skip
        return PreparedLaunch(command, dict(os.environ), ("docker", "rm", "--force", container))


class ProcessOracle:
    def __init__(
        self, isolation: Isolation, capture_limit_bytes: int = _DEFAULT_CAPTURE_LIMIT_BYTES
    ) -> None:
        self.isolation = isolation
        self._capture_limit = capture_limit_bytes

    async def observe(self, workdir: Path, launch: Launch) -> ProcessObservation:
        prepared = self.isolation.prepare(workdir, launch.argv)
        try:
            process = await asyncio.create_subprocess_exec(
                *prepared.command,
                cwd=workdir,
                env=prepared.environment,
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
            await _stop(process, prepared.stop_command)
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


async def _stop(process: asyncio.subprocess.Process, stop_command: tuple[str, ...] | None) -> None:
    if stop_command is not None:
        stopper = await asyncio.create_subprocess_exec(
            *stop_command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await stopper.wait()
    # macOS answers EPERM, not ESRCH, when the group's only member has already exited.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()
