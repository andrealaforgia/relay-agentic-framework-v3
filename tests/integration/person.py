"""A person at a terminal, for tests of commands that read from one."""

from __future__ import annotations

import fcntl
import os
import pty
import subprocess
import sys
import termios
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Ran:
    code: int
    printed: str
    # What was shown on the terminal itself: prompts, and what is about to be decided.
    shown: str
    # Standard output alone, where a command's JSON is.
    out: str = ""


def at_a_terminal(
    command: Sequence[str],
    typed: Sequence[str],
    environment: Mapping[str, str],
    directory: Path,
    within: float = 60.0,
    before_typing: Callable[[], None] | None = None,
) -> Ran:
    """Run `relay <command>` with a controlling terminal of its own, typing each of
    `typed` when the terminal asks for something (a prompt ending in a colon)."""
    master, slave = pty.openpty()

    def take_the_terminal() -> None:
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    child = subprocess.Popen(
        [sys.executable, "-m", "relay.cli.main", *command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=dict(environment),
        cwd=directory,
        start_new_session=True,
        preexec_fn=take_the_terminal,  # noqa: PLW1509 - the child needs a terminal of its own
    )
    os.set_blocking(master, False)
    assert child.stdout is not None
    os.set_blocking(child.stdout.fileno(), False)
    shown, printed, answered, to_type = b"", b"", 0, list(typed)
    started = quiet_since = time.monotonic()
    try:
        while child.poll() is None or time.monotonic() - quiet_since < 0.3:
            assert time.monotonic() - started < within, (
                f"still running: shown {shown!r}, printed {printed!r}"
            )
            heard = False
            for source in ("terminal", "output"):
                try:
                    more = (
                        os.read(master, 4096)
                        if source == "terminal"
                        else os.read(child.stdout.fileno(), 4096)
                    )
                except BlockingIOError, OSError:
                    more = b""
                if more:
                    heard = True
                    if source == "terminal":
                        shown += more
                    else:
                        printed += more
            if heard:
                quiet_since = time.monotonic()
            else:
                time.sleep(0.02)
            # Only once the terminal has gone quiet: a long piece of what is shown can
            # arrive in parts, and one of them can end in a colon that is no prompt. What
            # is typed before the real prompt is thrown away when the terminal is set to
            # read a passphrase, and the prompt then waits for ever.
            if (
                to_type
                and time.monotonic() - quiet_since > 0.15
                and shown.count(b": ") > answered
                and shown.rstrip(b" ").endswith(b":")
            ):
                answered = shown.count(b": ")
                if before_typing is not None:
                    # What happens while the person is looking at what they were shown.
                    before_typing()
                os.write(master, to_type.pop(0).encode() + b"\n")
        return Ran(child.wait(timeout=10), printed.decode(), shown.decode().replace("\r\n", "\n"))
    finally:
        child.kill()
        os.close(master)
        os.close(slave)


def with_no_terminal(
    command: Sequence[str], environment: Mapping[str, str], directory: Path, given: str = ""
) -> Ran:
    """Run `relay <command>` as a model would: no controlling terminal, and whatever it
    likes on standard input."""
    done = subprocess.run(
        [sys.executable, "-m", "relay.cli.main", *command],
        input=given.encode(),
        capture_output=True,
        env=dict(environment),
        cwd=directory,
        start_new_session=True,
        timeout=60,
        check=False,
    )
    return Ran(done.returncode, (done.stdout + done.stderr).decode(), "", done.stdout.decode())
