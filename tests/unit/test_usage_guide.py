"""What `docs/USAGE.md` tells a person to type is what Relay takes (3C-S4-1.11)."""

from __future__ import annotations

import re
from pathlib import Path

from relay.cli import deciding

GUIDE = (Path(__file__).resolve().parents[2] / "docs" / "USAGE.md").read_text()


def row(about: str) -> str:
    [found] = [line for line in GUIDE.splitlines() if line.startswith(f"| A **{about}**")]
    return found


def test_a_retry_is_given_a_reason_only_where_relay_takes_one() -> None:
    """A parked call and a failed final check are retried with no reason, and are refused
    one; a refused push is retried with one, and is refused without."""
    assert "`relay retry <handle>` to allow one more" in row("parked call")
    assert "--reason" not in row("parked call")
    assert "`relay retry <handle>` runs the check again" in row("failed final check")
    assert "--reason" not in row("failed final check")
    assert '`relay retry <handle> --reason "..."`' in row("refused push")


def test_every_command_the_guide_names_is_one_relay_has() -> None:
    # In a line of its own in a block of commands, or in the text.
    named = set(re.findall(r"(?:`|^)relay ([a-z][a-z-]*)", GUIDE, re.MULTILINE))
    known = {*deciding.VERBS, "feature", "up", "worker", "init", "passphrase", "policy",
             "requests", "doctor", "status", "inbox", "show", "roadmap", "ledger", "costs",
             "watch", "prepare", "confirm", "session"}  # fmt: skip
    assert named - known == set(), named - known
    assert {"init", "doctor", "policy", "up", "worker", "feature", "inbox", "show", "approve",
            "answer", "watch", "costs", "session", "confirm"} <= named  # fmt: skip
