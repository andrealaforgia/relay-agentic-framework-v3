"""The process oracle: trusted code that launches the subject and records what it observed."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Protocol

from relay.domain.evidence import Ineligibility, Launch, ProcessObservation

_DEFAULT_CAPTURE_LIMIT_BYTES = 4 * 1024 * 1024
_HOUSEKEEPING_TIMEOUT_SECONDS = 60


class CleanupFailed(Exception):
    """A subject may still be running. This must reach an operator, not be swallowed."""


class Isolation(Protocol):
    """How the subject is kept away from the supervisor that observes it."""

    name: str
    runtime_identity: str

    async def run(
        self, workdir: Path, launch: Launch, capture_limit: int
    ) -> ProcessObservation: ...


class ProcessOracle:
    def __init__(
        self, isolation: Isolation, capture_limit_bytes: int = _DEFAULT_CAPTURE_LIMIT_BYTES
    ) -> None:
        self.isolation = isolation
        self._capture_limit = capture_limit_bytes

    async def observe(self, workdir: Path, launch: Launch) -> ProcessObservation:
        return await self.isolation.run(workdir, launch, self._capture_limit)


class NativeIsolation:
    """Runs the subject as a child process with a scrubbed environment. It shares the
    supervisor's account and filesystem, so what it observes is diagnostic and never
    opens a gate."""

    name = "native"
    runtime_identity = "native"

    async def run(self, workdir: Path, launch: Launch, capture_limit: int) -> ProcessObservation:
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(workdir),
            "LANG": "C.UTF-8",
        }
        captured = await _run_captured(
            launch.argv,
            cwd=workdir,
            environment=environment,
            stdin=launch.stdin.encode(),
            timeout_seconds=launch.timeout_seconds,
            capture_limit=capture_limit,
        )
        return _observation(captured, captured.exit_code)


class ContainerIsolation:
    """Runs the subject in a disposable container: no network, read-only root and checkout,
    no capabilities, bounded memory, processes and CPU, and none of the host's environment
    or Docker client configuration."""

    name = "container"

    def __init__(self, image: str) -> None:
        if "@sha256:" not in image:
            raise ValueError(f"the runner image must be pinned by digest, not {image!r}")
        self.image = image
        self.runtime_identity = image

    async def run(self, workdir: Path, launch: Launch, capture_limit: int) -> ProcessObservation:
        container = f"relay-oracle-{uuid.uuid4().hex}"
        created = await self._docker(
            "create", "--interactive", "--name", container,
            # An init process, so the subject is not PID 1 (which ignores signals) and a
            # death by signal is reported as one.
            "--init",
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
            *launch.argv,
        )  # fmt: skip
        if created.exit_code != 0:
            return ProcessObservation(
                stderr=created.stderr, ineligibility=Ineligibility.NOT_EXECUTED
            )
        try:
            # Only the container's own streams pass through `start --attach`; anything the
            # Docker client had to say was said at `create` and is kept apart.
            attached = await _run_captured(
                ("docker", "start", "--attach", "--interactive", container),
                cwd=workdir,
                environment=_docker_environment(),
                stdin=launch.stdin.encode(),
                timeout_seconds=launch.timeout_seconds,
                capture_limit=capture_limit,
            )
            if attached.timed_out:
                return ProcessObservation(ineligibility=Ineligibility.TIMED_OUT)
            return _observation(attached, await self._exit_code(container, attached))
        finally:
            await self._remove(container)

    async def _exit_code(self, container: str, attached: _Captured) -> int | None:
        """The container's own exit status, from the engine. None when it never ran, or
        when the engine and the client disagree and the run cannot be trusted."""
        inspected = await self._docker(
            "inspect",
            "--format",
            "{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|{{.State.Error}}",
            container,
        )
        if inspected.exit_code != 0:
            return None
        status, exit_code, out_of_memory, error = inspected.stdout.decode().strip().split("|", 3)
        if status != "exited" or error or int(exit_code) != attached.exit_code:
            return None
        return -signal.SIGKILL if out_of_memory == "true" else int(exit_code)

    async def _remove(self, container: str) -> None:
        removed = await self._docker("rm", "--force", container)
        if removed.exit_code != 0 or removed.timed_out:
            raise CleanupFailed(
                f"could not remove {container}: {removed.stderr.decode(errors='replace').strip()}"
            )

    async def _docker(self, *arguments: str) -> _Captured:
        return await _run_captured(
            ("docker", *arguments),
            cwd=Path(tempfile.gettempdir()),
            environment=_docker_environment(),
            stdin=b"",
            timeout_seconds=_HOUSEKEEPING_TIMEOUT_SECONDS,
            capture_limit=_DEFAULT_CAPTURE_LIMIT_BYTES,
        )


@cache
def _docker_environment() -> dict[str, str]:
    """A Docker client environment that carries nothing of the operator's: an empty client
    configuration (no proxy settings, no registry credentials) and an explicit engine address."""
    host = (
        os.environ.get("DOCKER_HOST")
        or subprocess.run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "DOCKER_HOST": host,
        "DOCKER_CONFIG": tempfile.mkdtemp(prefix="relay-docker-config-"),
    }


@dataclass(frozen=True)
class _Captured:
    exit_code: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""
    overflowed: bool = False
    timed_out: bool = False
    launch_error: str | None = None


def _observation(captured: _Captured, exit_code: int | None) -> ProcessObservation:
    return ProcessObservation(
        exit_code=exit_code,
        stdout=captured.stdout,
        stderr=captured.stderr if captured.launch_error is None else captured.launch_error.encode(),
        ineligibility=_ineligibility(captured, exit_code),
    )


def _ineligibility(captured: _Captured, exit_code: int | None) -> Ineligibility | None:
    if captured.launch_error is not None or exit_code is None and not captured.timed_out:
        return Ineligibility.NOT_EXECUTED
    if captured.timed_out:
        return Ineligibility.TIMED_OUT
    assert exit_code is not None
    if captured.overflowed:
        return Ineligibility.OUTPUT_OVERFLOW
    # A status of 128 or more is how a shell or a container engine reports death by
    # signal. A subject could also return one on purpose; the two cannot be told apart,
    # so the run fails closed.
    if exit_code < 0 or exit_code >= 128:
        return Ineligibility.KILLED_BY_SIGNAL
    # 125 to 127 are what a launcher returns when it could not start the program.
    if exit_code >= 125:
        return Ineligibility.NOT_EXECUTED
    return None


async def _run_captured(
    command: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
    stdin: bytes,
    timeout_seconds: float,
    capture_limit: int,
) -> _Captured:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        return _Captured(launch_error=str(error))
    assert process.stdin and process.stdout and process.stderr
    try:
        async with asyncio.timeout(timeout_seconds):
            (
                _,
                (stdout, stdout_overflowed),
                (stderr, stderr_overflowed),
                exit_code,
            ) = await asyncio.gather(
                _feed(process.stdin, stdin),
                _read_up_to(process.stdout, capture_limit),
                _read_up_to(process.stderr, capture_limit),
                process.wait(),
            )
    except TimeoutError:
        return _Captured(timed_out=True)
    finally:
        # Also reached on cancellation: the process must not outlive its observer.
        await _kill(process)
    return _Captured(exit_code, stdout, stderr, stdout_overflowed or stderr_overflowed)


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


async def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    # macOS answers EPERM, not ESRCH, when the group's only member has already exited.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()
