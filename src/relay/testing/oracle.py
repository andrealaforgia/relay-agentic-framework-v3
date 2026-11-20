"""The process oracle: trusted code that launches the subject and records what it observed."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import shutil
import signal
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Protocol

from relay.domain.evidence import Execution, Ineligibility, Launch, ProcessObservation

_DEFAULT_CAPTURE_LIMIT_BYTES = 4 * 1024 * 1024
_HOUSEKEEPING_TIMEOUT_SECONDS = 60


class CleanupFailed(Exception):
    """A subject may still be running. This must reach an operator, not be swallowed."""


@dataclass(frozen=True)
class Run:
    observation: ProcessObservation
    runtime_identity: str


class Isolation(Protocol):
    """How the subject is kept away from the supervisor that observes it."""

    name: str

    async def run(self, workdir: Path, launch: Launch, capture_limit: int) -> Run: ...


class ProcessOracle:
    def __init__(
        self, isolation: Isolation, capture_limit_bytes: int = _DEFAULT_CAPTURE_LIMIT_BYTES
    ) -> None:
        self.isolation = isolation
        self._capture_limit = capture_limit_bytes

    async def observe(self, workdir: Path, launch: Launch) -> ProcessObservation:
        return (await self.isolation.run(workdir, launch, self._capture_limit)).observation

    async def execute(self, revision: str, workdir: Path, launch: Launch) -> Execution:
        run = await self.isolation.run(workdir, launch, self._capture_limit)
        return Execution(revision, self.isolation.name, run.runtime_identity, run.observation)


class NativeIsolation:
    """Runs the subject as a child process with a scrubbed environment. It shares the
    supervisor's account and filesystem, so what it observes is diagnostic and never
    opens a gate."""

    name = "native"

    async def run(self, workdir: Path, launch: Launch, capture_limit: int) -> Run:
        environment = {"PATH": _trusted_path(), "HOME": str(workdir), "LANG": "C.UTF-8"}
        captured = await _run_captured(
            launch.argv,
            cwd=workdir,
            environment=environment,
            stdin=launch.stdin.encode(),
            timeout_seconds=launch.timeout_seconds,
            capture_limit=capture_limit,
        )
        return Run(_observation(captured, captured.exit_code), "native")


@dataclass(frozen=True)
class ContainerState:
    """What the engine itself says about a container after it stopped."""

    status: str
    exit_code: int
    out_of_memory: bool
    error: str
    image_id: str


class ContainerIsolation:
    """Runs the subject in a disposable container on the local engine: no network, read-only
    root and checkout, no capabilities, bounded memory, processes and CPU, and none of the
    host's environment or Docker client configuration."""

    name = "container"
    # Fixed on purpose, including the user: nothing here may vary with the host or the
    # account the supervisor runs under.
    settings: tuple[str, ...] = (
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
        "--user", "65534:65534",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
    )  # fmt: skip

    def __init__(self, image: str) -> None:
        if "@sha256:" not in image:
            raise ValueError(f"the runner image must be pinned by digest, not {image!r}")
        self.image = image

    async def run(self, workdir: Path, launch: Launch, capture_limit: int) -> Run:
        container = f"relay-oracle-{uuid.uuid4().hex}"
        try:
            # Removal below covers creation too: the engine may have created the container
            # even if this client was cancelled before hearing so.
            return await self._run(container, workdir, launch, capture_limit)
        finally:
            await self._remove(container)

    async def _run(self, container: str, workdir: Path, launch: Launch, capture_limit: int) -> Run:
        # The subject runs as a fixed unprivileged user, the same on every runner host,
        # so it must be able to read a checkout made by whoever runs the supervisor.
        if workdir.is_dir():
            await asyncio.to_thread(_make_world_readable, workdir)
        log_limit = max(capture_limit * 2, 1024 * 1024)
        created = await self._docker(
            "create", "--interactive", "--name", container,
            *self.settings,
            # Bound what the engine stores of the subject's output; a rotated log no longer
            # matches the attached capture, so such a run is refused rather than trusted.
            "--log-driver", "json-file",
            "--log-opt", f"max-size={log_limit}",
            "--log-opt", "max-file=1",
            # --mount, unlike --volume, fails when the source is missing instead of
            # creating an empty directory and running against that.
            "--mount", f"type=bind,source={workdir},target=/subject,readonly",
            "--workdir", "/subject",
            self.image,
            *launch.argv,
        )  # fmt: skip
        if created.exit_code != 0:
            return Run(
                ProcessObservation(stderr=created.stderr, ineligibility=Ineligibility.NOT_EXECUTED),
                self._identity(self.image, log_limit),
            )
        created_state = await self._state(container)
        identity = self._identity(
            created_state.image_id if created_state else self.image, log_limit
        )
        # Attaching delivers standard input and waits for the subject to finish. What the
        # subject wrote is then read back from the engine, a second and independent capture.
        attached = await self._docker(
            "start", "--attach", "--interactive", container,
            stdin=launch.stdin.encode(),
            timeout_seconds=launch.timeout_seconds,
            capture_limit=capture_limit,
        )  # fmt: skip
        if attached.timed_out:
            return Run(ProcessObservation(ineligibility=Ineligibility.TIMED_OUT), identity)
        state = await self._state(container)
        logged = await self._docker("logs", container, capture_limit=capture_limit)
        return Run(reconcile_container_run(attached, logged, state), identity)

    def _identity(self, image: str, log_limit: int) -> str:
        """Everything about the runtime that could change what a subject does: the resolved,
        platform-specific image and the settings the container was created with. A red and a
        green are comparable only when this is the same for both."""
        settings = hashlib.sha256(repr((self.settings, log_limit)).encode()).hexdigest()[:16]
        return f"{image};settings={settings}"

    async def _state(self, container: str) -> ContainerState | None:
        inspected = await self._docker(
            "inspect",
            "--format",
            "{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|{{.Image}}|{{.State.Error}}",
            container,
        )
        if inspected.exit_code != 0:
            return None
        status, exit_code, out_of_memory, image_id, error = (
            inspected.stdout.decode().strip().split("|", 4)
        )
        return ContainerState(status, int(exit_code), out_of_memory == "true", error, image_id)

    async def _remove(self, container: str) -> None:
        removed = await self._docker("rm", "--force", container)
        gone = removed.exit_code == 0 or b"No such container" in removed.stderr
        if not gone:
            raise CleanupFailed(
                f"could not remove {container}: {removed.stderr.decode(errors='replace').strip()}"
            )

    async def _docker(
        self,
        *arguments: str,
        stdin: bytes = b"",
        timeout_seconds: float = _HOUSEKEEPING_TIMEOUT_SECONDS,
        capture_limit: int = _DEFAULT_CAPTURE_LIMIT_BYTES,
    ) -> Captured:
        executable, environment = _docker_client()
        return await _run_captured(
            (executable, *arguments),
            # Never the checkout: nothing the subject's repository contains may be
            # found by the client's own lookups.
            cwd=_TRUSTED_DIRECTORY,
            environment=environment,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            capture_limit=capture_limit,
        )


def reconcile_container_run(
    attached: Captured, logged: Captured, state: ContainerState | None
) -> ProcessObservation:
    """Admit a container run only when the engine's account of it and the attached client's
    agree: it ran, it exited, and both captures hold the same complete output. A matching
    exit code alone does not show the attached capture was complete."""
    never_ran = state is None or state.status != "exited" or bool(state.error)
    capture_failed = logged.exit_code != 0 or logged.timed_out
    overflowed = attached.overflowed or logged.overflowed
    if never_ran or capture_failed:
        return ProcessObservation(stderr=attached.stderr, ineligibility=Ineligibility.NOT_EXECUTED)
    assert state is not None
    disagree = attached.exit_code != state.exit_code or (
        not overflowed and (attached.stdout, attached.stderr) != (logged.stdout, logged.stderr)
    )
    if disagree:
        return ProcessObservation(
            stderr=b"the attached capture and the engine's record of the run disagree",
            ineligibility=Ineligibility.NOT_EXECUTED,
        )
    exit_code = -signal.SIGKILL if state.out_of_memory else state.exit_code
    return _observation(Captured(exit_code, logged.stdout, logged.stderr, overflowed), exit_code)


_TRUSTED_DIRECTORY = Path("/")


def _make_world_readable(directory: Path) -> None:
    for root, _, files in os.walk(directory):
        os.chmod(root, os.stat(root).st_mode | 0o555)
        for name in files:
            path = os.path.join(root, name)
            if not os.path.islink(path):
                os.chmod(path, os.stat(path).st_mode | 0o444)


def _trusted_path() -> str:
    """The supervisor's PATH without empty or relative entries, which would resolve
    against whatever directory a process happens to run in."""
    entries = os.environ.get("PATH", os.defpath).split(os.pathsep)
    return os.pathsep.join(entry for entry in entries if os.path.isabs(entry))


@cache
def _docker_client() -> tuple[str, dict[str, str]]:
    """An absolute Docker executable and an environment that carries nothing of the
    operator's: an empty client configuration (no proxy settings, no registry credentials)
    and an explicit address for a local engine."""
    executable = shutil.which("docker", path=_trusted_path())
    if executable is None:
        raise RuntimeError("docker was not found on any absolute PATH entry")
    host = (
        os.environ.get("DOCKER_HOST")
        or subprocess.run(
            [executable, "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            check=True,
            capture_output=True,
            text=True,
            cwd=_TRUSTED_DIRECTORY,
        ).stdout.strip()
    )
    if not host.startswith("unix://"):
        # A remote engine would resolve the checkout's path on its own host and run
        # something other than the revision the evidence names.
        raise RuntimeError(f"the oracle needs a local Docker engine, not {host!r}")
    environment = {
        "PATH": _trusted_path(),
        "DOCKER_HOST": host,
        "DOCKER_CONFIG": tempfile.mkdtemp(prefix="relay-docker-config-"),
    }
    return executable, environment


@dataclass(frozen=True)
class Captured:
    exit_code: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""
    overflowed: bool = False
    timed_out: bool = False
    launch_error: str | None = None


def _observation(captured: Captured, exit_code: int | None) -> ProcessObservation:
    return ProcessObservation(
        exit_code=exit_code,
        stdout=captured.stdout,
        stderr=captured.stderr if captured.launch_error is None else captured.launch_error.encode(),
        ineligibility=_ineligibility(captured, exit_code),
    )


def _ineligibility(captured: Captured, exit_code: int | None) -> Ineligibility | None:
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
) -> Captured:
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
        return Captured(launch_error=str(error))
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
        return Captured(timed_out=True)
    finally:
        # Also reached on cancellation: the process must not outlive its observer.
        await _kill(process)
    return Captured(exit_code, stdout, stderr, stdout_overflowed or stderr_overflowed)


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
