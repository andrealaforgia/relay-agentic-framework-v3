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
import unicodedata
from contextlib import suppress

TERMINAL = "/dev/tty"


class NoTerminal(Exception):
    """This process has no controlling terminal to read a passphrase from."""


def read_secret(prompt: str, terminal: str = TERMINAL) -> str:
    """Show `prompt` on the controlling terminal and read one line from it, unechoed."""
    typed = _read(prompt, terminal, shown=False)
    return "" if typed is None else typed


def read_line(prompt: str, terminal: str = TERMINAL) -> str | None:
    """Show `prompt` on the controlling terminal and read one line the person types there,
    shown as they type it. None when the person ends the input (Ctrl-D): there is no more.
    A conversation is with a person at a terminal, as a passphrase is: what a model that
    runs the command puts on standard input is not read."""
    return _read(prompt, terminal, shown=True)


def _read(prompt: str, terminal: str, *, shown: bool) -> str | None:
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
        if shown:
            quiet[3] |= termios.ECHO
        else:
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
            if not shown:
                with suppress(OSError):
                    os.write(descriptor, b"\n")
        if shown and not typed:
            return None
        return typed.decode(errors="replace").rstrip("\r\n")
    finally:
        os.close(descriptor)


def visible(text: str) -> str:
    """Text from somewhere else, made safe to put in front of a person who is about to
    confirm something: a model wrote it, or a repository said it. Every control character
    is shown as what it is, and none is sent to the terminal: text that could move the
    cursor, clear the screen or rewrite a line above could change what the person sees
    of the action they are asked to authorise. A line break is one of them."""
    return "".join(
        character
        if unicodedata.category(character)[0] != "C"
        else f"\\x{ord(character):02x}"
        if ord(character) < 256
        else f"\\u{ord(character):04x}"
        for character in text
    )


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
