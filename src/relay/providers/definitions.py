"""The canonical agent definitions, used as installed and never rewritten.

For every invocation Relay records which definition it used: its path, the hash of its
content, and the revision of the repository it is kept in, if it is kept in one. The
content that is hashed is the content that is handed to the provider, so what was
recorded is what ran, whatever is on the host a moment later.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# Specification section 4.
DEFAULT_DEFINITIONS = {
    "analyst": "alf-user-story-writer",
    "verifier": "alf-atdd-developer",
    "builder": "alf-atdd-developer",
    "qa": "alf-test-design-reviewer",
    "sentinel": "alf-security-assessor",
}

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    path: Path
    sha256: str
    # The commit its repository was at, and whether the file differs from that commit.
    revision: str | None
    dirty: bool
    description: str
    prompt: str


def search_path(configured: Sequence[Path], *, project: Path | None, home: Path) -> list[Path]:
    """Where definitions are looked for, first match wins: what was configured, then the
    project's own, then the user's."""
    return [
        *configured,
        *([project / ".claude" / "agents"] if project is not None else []),
        home / ".claude" / "agents",
    ]


def find_definition(name: str, directories: Sequence[Path]) -> AgentDefinition:
    if not _NAME.fullmatch(name):
        raise ValueError(f"not an agent name: {name!r}")
    for directory in directories:
        path = (directory / f"{name}.md").resolve()
        if path.is_file():
            content = path.read_bytes()
            description, prompt = _split(content.decode())
            revision, dirty = _revision(path)
            return AgentDefinition(
                name=name,
                path=path,
                sha256=hashlib.sha256(content).hexdigest(),
                revision=revision,
                dirty=dirty,
                description=description,
                prompt=prompt,
            )
    raise LookupError(f"no definition of {name} is installed in any of the places searched")


def _split(text: str) -> tuple[str, str]:
    """The description from the front matter, and everything after the front matter. The
    tools, model and skills a definition asks for are not read: Relay decides those for
    the role, and a definition cannot widen them."""
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---\n", 4)
    if end < 0:
        return "", text
    description = ""
    for line in text[4:end].splitlines():
        if line.startswith("description:"):
            description = line.removeprefix("description:").strip().strip("\"'")
    return description, text[end + 5 :].lstrip("\n")


def _revision(path: Path) -> tuple[str | None, bool]:
    def git(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(path.parent), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )

    head = git("rev-parse", "HEAD")
    if head.returncode != 0:
        return None, False
    # Anything Git has to say about the file means it is not the file of that commit:
    # modified, staged, or never committed.
    status = git("status", "--porcelain", "--", path.name)
    return head.stdout.strip(), status.returncode != 0 or bool(status.stdout.strip())
