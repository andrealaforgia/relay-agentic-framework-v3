"""A yes-or-no question to Git has three outcomes: yes, no, and no answer. Only the first
two are answers."""

import asyncio
import os
import threading
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


async def test_a_git_that_does_not_exist_never_ran(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkout, "_TRUSTED_GIT", ("/nonexistent/git",))

    with pytest.raises(NotStarted, match="could not be started"):
        await git_result("push", "somewhere")


async def test_a_failure_before_anything_is_launched_never_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_file_descriptors(*_: object, **__: object) -> None:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(checkout.tempfile, "TemporaryFile", no_file_descriptors)

    with pytest.raises(NotStarted):
        await git_result("push", "somewhere")


async def test_a_launch_failure_that_does_not_prove_git_stayed_put_is_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launcher reads an acknowledgement from the child after starting it. A failure
    there arrives with Git possibly already running."""

    def acknowledgement_lost(*_: object, **__: object) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(checkout.subprocess, "Popen", acknowledgement_lost)

    with pytest.raises(RemoteUnavailable, match="may or may not have run") as failure:
        await git_result("push", "somewhere")

    assert not isinstance(failure.value, NotStarted)


async def test_a_git_stopped_for_taking_too_long_leaves_nothing_behind(tmp_path: Path) -> None:
    """Its children may hold its output open for as long as they like."""
    (tmp_path / "git-remote-slow").write_text("#!/bin/sh\nsleep 5 &\nwait\n")
    (tmp_path / "git-remote-slow").chmod(0o755)
    environment = dict(checkout._GIT_ENVIRONMENT)
    environment["PATH"] = f"{tmp_path}:{environment['PATH']}"
    before = (threading.active_count(), len(os.listdir("/dev/fd")))

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(checkout, "_GIT_ENVIRONMENT", environment)
        for _ in range(3):
            with pytest.raises(RemoteUnavailable, match="no answer in time"):
                await git_result("ls-remote", f"slow://{tmp_path.name}", timeout=0.3)

    assert (threading.active_count(), len(os.listdir("/dev/fd"))) == before
    still_running = await asyncio.create_subprocess_exec(
        "pgrep", "-f", f"ls-remote slow://{tmp_path.name}", stdout=asyncio.subprocess.DEVNULL
    )
    assert await still_running.wait() == 1, "a Git that was given up on is still running"
