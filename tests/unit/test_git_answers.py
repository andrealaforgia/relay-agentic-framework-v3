"""A yes-or-no question to Git has three outcomes: yes, no, and no answer. Only the first
two are answers."""

import asyncio
import os
import signal
import tempfile
import threading
import time
from pathlib import Path
from typing import Self

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


async def test_an_exit_status_nobody_could_collect_is_not_a_success() -> None:
    """With child signals ignored the status of a finished Git is lost. Git here fails
    (there is no such subcommand); that must never come back as exit status zero."""
    assert (await git_result("no-such-subcommand"))[0] == 1
    previous = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    try:
        with pytest.raises(RemoteUnavailable, match="exit status could not be collected"):
            await git_result("no-such-subcommand")
    finally:
        signal.signal(signal.SIGCHLD, previous)


async def test_output_that_cannot_be_read_back_is_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = tempfile.TemporaryFile

    class Unreadable:
        def __init__(self) -> None:
            self._file = real()

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            self._file.close()

        def fileno(self) -> int:
            return self._file.fileno()

        def seek(self, _: int) -> None:
            raise OSError(5, "Input/output error")

    monkeypatch.setattr(checkout.tempfile, "TemporaryFile", Unreadable)

    with pytest.raises(RemoteUnavailable, match="output could not be read"):
        await git_result("--version")


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
