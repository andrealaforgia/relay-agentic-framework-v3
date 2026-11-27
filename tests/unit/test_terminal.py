"""A passphrase is read from the controlling terminal and from nowhere else."""

from __future__ import annotations

import fcntl
import os
import pty
import select
import subprocess
import sys
import termios
import time

READS = (
    "import sys\n"
    "from relay.cli.terminal import NoTerminal, read_secret\n"
    "try:\n"
    "    print('READ', repr(read_secret('Passphrase: ')))\n"
    "except NoTerminal as none:\n"
    "    print('REFUSED', none)\n"
    "    sys.exit(3)\n"
)


def shown_on(master: int, until: bytes, within: float = 10.0) -> bytes:
    """What the terminal is shown, up to `until`. One deadline for the whole of it, and a
    terminal that has ended is said to have ended: this never waits for ever."""
    shown, deadline = b"", time.monotonic() + within
    while until not in shown:
        left = deadline - time.monotonic()
        assert left > 0, f"the terminal showed only {shown!r} in {within} seconds"
        ready, _, _ = select.select([master], [], [], left)
        if not ready:
            continue
        try:
            more = os.read(master, 1024)
        except OSError:
            more = b""
        assert more, f"the terminal ended, having shown only {shown!r}"
        shown += more
    return shown


def test_what_is_typed_at_the_terminal_is_read_and_not_shown_and_standard_input_is_not_asked() -> (
    None
):
    master, slave = pty.openpty()

    def take_the_terminal() -> None:
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    child = subprocess.Popen(
        [sys.executable, "-c", READS],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        start_new_session=True,
        preexec_fn=take_the_terminal,  # noqa: PLW1509 - the child needs a terminal of its own
    )
    try:
        assert child.stdin is not None and child.stdout is not None
        child.stdin.write(b"given on standard input\n")
        child.stdin.close()
        assert shown_on(master, b"Passphrase: ").endswith(b"Passphrase: ")

        os.write(master, b"typed by the person\n")

        # What the child prints and what the terminal is shown are both taken as they
        # come: a process closing its terminal waits for what it wrote there to be read.
        os.set_blocking(master, False)
        os.set_blocking(child.stdout.fileno(), False)
        after, printed, quiet_since = b"", b"", time.monotonic()
        while child.poll() is None or time.monotonic() - quiet_since < 0.3:
            assert time.monotonic() - quiet_since < 20, "the child did not end"
            heard = b""
            for descriptor in (master, child.stdout.fileno()):
                try:
                    more = os.read(descriptor, 1024)
                except BlockingIOError, OSError:
                    more = b""
                if descriptor == master:
                    after += more
                else:
                    printed += more
                heard += more
            if heard:
                quiet_since = time.monotonic()
            else:
                time.sleep(0.02)
        assert printed.decode().strip() == "READ 'typed by the person'"
        assert child.wait(timeout=10) == 0
        assert b"typed by the person" not in after  # nothing typed was echoed
    finally:
        child.kill()
        os.close(master)
        os.close(slave)


def test_a_process_with_no_controlling_terminal_is_refused_whatever_it_is_given() -> None:
    done = subprocess.run(
        [sys.executable, "-c", READS],
        input=b"given on standard input\n",
        capture_output=True,
        start_new_session=True,
        env={**os.environ, "RELAY_PASSPHRASE": "given in the environment"},
        timeout=30,
        check=False,
    )

    assert done.returncode == 3
    assert done.stdout.decode().startswith("REFUSED this needs a person at a terminal")
