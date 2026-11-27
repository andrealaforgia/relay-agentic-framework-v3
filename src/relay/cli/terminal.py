"""Reading from the person at the terminal, and from nowhere else.

A passphrase is read from the controlling terminal of the process, with nothing shown as
it is typed. It is never taken from an argument, the environment, standard input or a
file: each of those is something a model that runs the command can supply.

This is not proof of a person. A model given a pseudo-terminal has a controlling terminal
too. It still has to know the passphrase.
"""

from __future__ import annotations

import os
import termios
from contextlib import suppress

TERMINAL = "/dev/tty"


class NoTerminal(Exception):
    """This process has no controlling terminal to read a passphrase from."""


def read_secret(prompt: str, terminal: str = TERMINAL) -> str:
    """Show `prompt` on the controlling terminal and read one line from it, unechoed."""
    try:
        descriptor = os.open(terminal, os.O_RDWR | os.O_NOCTTY)
    except OSError as none:
        raise NoTerminal(
            "this needs a person at a terminal: there is no controlling terminal to read "
            "the passphrase from, and it is read from nowhere else"
        ) from none
    try:
        try:
            before = termios.tcgetattr(descriptor)
        except termios.error as not_one:
            raise NoTerminal(f"{terminal} is not a terminal") from not_one
        quiet = list(before)
        quiet[3] &= ~termios.ECHO  # what is typed is not shown
        quiet[3] |= termios.ICANON  # and is read a line at a time
        try:
            termios.tcsetattr(descriptor, termios.TCSAFLUSH, quiet)
            os.write(descriptor, prompt.encode())
            typed = bytearray()
            while not typed.endswith(b"\n"):
                more = os.read(descriptor, 1024)
                if not more:
                    break
                typed += more
        finally:
            with suppress(termios.error):
                termios.tcsetattr(descriptor, termios.TCSAFLUSH, before)
            with suppress(OSError):
                os.write(descriptor, b"\n")
        return typed.decode().rstrip("\r\n")
    finally:
        os.close(descriptor)


def say(what: str, terminal: str = TERMINAL) -> None:
    """Show something on the controlling terminal itself: what is about to be decided is
    shown where the passphrase is then read, not on an output a model may have replaced."""
    try:
        descriptor = os.open(terminal, os.O_WRONLY | os.O_NOCTTY)
    except OSError as none:
        raise NoTerminal(
            "this needs a person at a terminal: there is no controlling terminal to show "
            "what is being decided on, or to read the passphrase from"
        ) from none
    try:
        os.write(descriptor, (what if what.endswith("\n") else what + "\n").encode())
    finally:
        os.close(descriptor)
