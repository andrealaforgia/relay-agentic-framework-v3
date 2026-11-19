"""Pristine checkouts of a pinned revision. Never a branch name: a name can move."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

_FULL_COMMIT_HASH = re.compile(r"[0-9a-f]{40}")
# No hooks, no user or system configuration, no credential prompts: this runs with
# the supervisor's authority on content the supervisor does not trust.
_TRUSTED_GIT = ("git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never")
_GIT_ENVIRONMENT = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
}


class CheckoutFailed(Exception):
    pass


async def pristine_checkout(repository: str, revision: str, into: Path) -> Path:
    if not _FULL_COMMIT_HASH.fullmatch(revision):
        raise CheckoutFailed(f"a revision must be a 40-character commit hash, not {revision!r}")
    await _git("clone", "--quiet", "--no-checkout", "--", repository, str(into))
    await _git("-C", str(into), "checkout", "--quiet", "--detach", revision)
    head = await _git("-C", str(into), "rev-parse", "HEAD")
    if head != revision:
        raise CheckoutFailed(f"checked out {head}, expected {revision}")
    return into


async def _git(*arguments: str) -> str:
    process = await asyncio.create_subprocess_exec(
        *_TRUSTED_GIT,
        *arguments,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_GIT_ENVIRONMENT,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise CheckoutFailed(
            f"git {arguments[0]} failed: {stderr.decode(errors='replace').strip()}"
        )
    return stdout.decode().strip()
