"""A yes-or-no question to Git has three outcomes: yes, no, and no answer. Only the first
two are answers."""

import asyncio
import os
import signal
import tempfile
import threading
import time
from pathlib import Path

import pytest

from relay.git import checkout
from relay.git.checkout import (
    CheckoutFailed,
    NotStarted,
    RemoteUnavailable,
    git_answers_yes,
    git_result,
)


async def test_exit_zero_is_yes_and_exit_one_is_no(tmp_path: Path) -> None:
    assert await git_answers_yes("-C", str(tmp_path), "init", "--quiet") is True
    assert await git_answers_yes("-C", str(tmp_path), "cat-file", "-e", "0" * 39 + "1") is False


async def test_any_other_exit_is_git_failing_to_answer(tmp_path: Path) -> None:
    with pytest.raises(CheckoutFailed, match="cat-file"):
        await git_answers_yes("-C", str(tmp_path / "not-a-repository"), "cat-file", "-e", "HEAD")


async def test_a_git_that_cannot_be_found_never_ran(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checkout, "_GIT_ENVIRONMENT", {"PATH": str(tmp_path)})

    with pytest.raises(NotStarted, match="was not found"):
        await git_result("push", "somewhere")


async def test_a_failure_before_anything_is_launched_never_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_file_descriptors(*_: object, **__: object) -> None:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(checkout.tempfile, "TemporaryFile", no_file_descriptors)

    with pytest.raises(NotStarted):
        await git_result("push", "somewhere")


@pytest.mark.parametrize(
    "failure",
    [OSError(5, "Input/output error"), FileNotFoundError(2, "No such file", "some-file")],
    ids=["no filename", "names a file"],
)
async def test_no_error_from_the_launch_itself_is_read_as_git_not_having_run(
    monkeypatch: pytest.MonkeyPatch, failure: OSError
) -> None:
    """The launcher can raise with the child already running Git, and an error raised on
    this side at that moment can look exactly like the child failing to start."""

    def launch_fails(*_: object, **__: object) -> None:
        raise failure

    monkeypatch.setattr(checkout.subprocess, "Popen", launch_fails)

    with pytest.raises(RemoteUnavailable, match="may or may not have run") as raised:
        await git_result("push", "somewhere")

    assert not isinstance(raised.value, NotStarted)


async def test_git_is_not_launched_where_its_exit_status_would_be_discarded() -> None:
    """With child signals ignored the system reaps a finished Git by itself. Its status
    is lost and its process id is free for reuse, so Relay refuses to launch at all."""
    assert (await git_result("no-such-subcommand"))[0] == 1
    previous = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    try:
        with pytest.raises(NotStarted, match="discards its children"):
            await git_result("no-such-subcommand")
    finally:
        signal.signal(signal.SIGCHLD, previous)


async def test_an_exit_status_someone_else_collected_is_not_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git here fails (there is no such subcommand). If its status is taken by someone
    else, that must never come back as exit status zero."""
    real = os.waitpid

    def taken_by_someone_else(pid: int, options: int) -> tuple[int, int]:
        real(pid, 0)
        raise ChildProcessError(10, "No child processes")

    monkeypatch.setattr(checkout.os, "waitpid", taken_by_someone_else)

    with pytest.raises(RemoteUnavailable, match="exit status could not be collected"):
        await git_result("no-such-subcommand")


@pytest.mark.parametrize("reaped_by", ["someone else", "the cleanup itself"])
async def test_a_git_no_longer_running_when_its_caller_is_cancelled_is_sent_no_signal(
    monkeypatch: pytest.MonkeyPatch, reaped_by: str
) -> None:
    """Once it has been reaped its process id may belong to something else."""
    real = os.waitpid
    asked = asyncio.Event()
    signals: list[int] = []

    def finished_meanwhile(pid: int, options: int) -> tuple[int, int]:
        if not asked.is_set():
            asked.set()
            return 0, 0  # still running, as far as the first look can tell
        collected = real(pid, 0)
        if reaped_by == "someone else":
            raise ChildProcessError(10, "No child processes")
        return collected

    monkeypatch.setattr(checkout.os, "waitpid", finished_meanwhile)
    monkeypatch.setattr(checkout.os, "kill", lambda pid, _: signals.append(pid))
    running = asyncio.create_task(git_result("--version"))
    await asked.wait()

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert signals == []


_A_REAL_FILE = tempfile.TemporaryFile


class Capture:
    """A real capture file that can be told to fail when read back or when closed."""

    def __init__(self, *, unreadable: bool = False, unclosable: bool = False) -> None:
        self._file = _A_REAL_FILE()
        self._unreadable = unreadable
        self._unclosable = unclosable

    def fileno(self) -> int:
        return self._file.fileno()

    def seek(self, position: int) -> None:
        if self._unreadable:
            raise OSError(5, "Input/output error")
        self._file.seek(position)

    def read(self) -> bytes:
        return self._file.read()

    def close(self) -> None:
        self._file.close()
        if self._unclosable:
            raise OSError(5, "Input/output error")


async def test_output_that_cannot_be_read_back_is_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkout.tempfile, "TemporaryFile", lambda: Capture(unreadable=True))

    with pytest.raises(RemoteUnavailable, match="output could not be read"):
        await git_result("--version")


async def test_output_files_that_cannot_be_closed_after_a_clean_run_are_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkout.tempfile, "TemporaryFile", lambda: Capture(unclosable=True))

    with pytest.raises(RemoteUnavailable, match="could not be closed"):
        await git_result("--version")


async def test_a_cancellation_is_still_a_cancellation_when_tidying_up_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "git-remote-slow").write_text("#!/bin/sh\nsleep 5\n")
    (tmp_path / "git-remote-slow").chmod(0o755)
    environment = dict(checkout._GIT_ENVIRONMENT)
    environment["PATH"] = f"{tmp_path}:{environment['PATH']}"
    monkeypatch.setattr(checkout, "_GIT_ENVIRONMENT", environment)
    monkeypatch.setattr(checkout.tempfile, "TemporaryFile", lambda: Capture(unclosable=True))
    running = asyncio.create_task(git_result("ls-remote", "slow://nowhere"))
    await asyncio.sleep(0.3)

    running.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await running

    assert running.cancelled()
    assert any("also failed" in note for note in cancelled.value.__notes__)


async def test_a_git_proven_not_to_have_started_stays_so_when_tidying_up_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second output file cannot be made, and then the first cannot be closed."""
    made: list[Capture] = []
    launches: list[object] = []

    def first_only() -> Capture:
        if made:
            raise OSError(24, "Too many open files")
        made.append(Capture(unclosable=True))
        return made[0]

    monkeypatch.setattr(checkout.tempfile, "TemporaryFile", first_only)
    monkeypatch.setattr(checkout.subprocess, "Popen", lambda *_, **__: launches.append(1))

    with pytest.raises(NotStarted) as never_started:
        await git_result("push", "somewhere")

    assert launches == []
    assert any("also failed" in note for note in never_started.value.__notes__)


async def test_a_git_stopped_for_taking_too_long_leaves_nothing_behind(tmp_path: Path) -> None:
    """Its children may hold its output open for as long as they like."""
    (tmp_path / "git-remote-slow").write_text("#!/bin/sh\nsleep 5 &\nwait\n")
    (tmp_path / "git-remote-slow").chmod(0o755)
    environment = dict(checkout._GIT_ENVIRONMENT)
    environment["PATH"] = f"{tmp_path}:{environment['PATH']}"
    before = (threading.active_count(), len(os.listdir("/dev/fd")))
    started = time.monotonic()

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(checkout, "_GIT_ENVIRONMENT", environment)
        for _ in range(3):
            with pytest.raises(RemoteUnavailable, match="no answer in time"):
                await git_result("ls-remote", f"slow://{tmp_path.name}", timeout=0.3)

    assert time.monotonic() - started < 4, "giving up waited for Git to finish by itself"
    assert (threading.active_count(), len(os.listdir("/dev/fd"))) == before
    still_running = await asyncio.create_subprocess_exec(
        "pgrep", "-f", f"ls-remote slow://{tmp_path.name}", stdout=asyncio.subprocess.DEVNULL
    )
    assert await still_running.wait() == 1, "a Git that was given up on is still running"
