"""Which project a command means.

Never guessed. A command names its project, or the environment does, or it is run inside
a checkout that is one registered project's and no other's. Anything else is refused,
with the candidates named.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from relay.git.checkout import canonical_repository
from relay.persistence.database import Pool


class NoProject(Exception):
    """No one project could be told from what the command was given."""


@dataclass(frozen=True)
class Checkout:
    """What a working directory is a checkout of, as Git has it."""

    root: Path
    branch: str | None
    remotes: tuple[str, ...]


def checkout_at(directory: Path) -> Checkout | None:
    """The checkout this directory is inside, or None if it is inside none."""

    def git(*arguments: str) -> str | None:
        done = subprocess.run(
            ["git", "-C", str(directory), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        # Git ends what it says with one newline, and that is all that is taken off: a
        # path or an address may itself end in a space, and is another place without it.
        return done.stdout.removesuffix("\n") if done.returncode == 0 else None

    root = git("rev-parse", "--show-toplevel")
    if root is None:
        return None
    remotes = []
    for name in filter(None, (git("remote") or "").split("\n")):
        address = git("remote", "get-url", name)
        if address:
            remotes.append(address)
    return Checkout(
        Path(root), git("symbolic-ref", "--short", "-q", "HEAD") or None, tuple(remotes)
    )


async def select_project(
    pool: Pool, named: str | None, environ: Mapping[str, str], directory: Path
) -> str:
    """The project meant, by: what the command names; what `RELAY_PROJECT` names; the
    checkout the command was run in, if its remote and branch are one project's."""
    async with pool.connection() as connection:
        projects = await (
            await connection.execute(
                "SELECT project_id, repository, trunk FROM projects ORDER BY project_id"
            )
        ).fetchall()
    known = {row["project_id"]: row for row in projects}
    asked, how = (named, "--project") if named else (environ.get("RELAY_PROJECT"), "RELAY_PROJECT")
    if asked:
        if asked not in known:
            raise NoProject(
                f"{how} names {asked!r}, which is not a registered project. "
                f"Registered: {', '.join(known) or 'none'}"
            )
        return asked
    checkout = checkout_at(directory)
    if checkout is None:
        raise NoProject(
            "which project is meant was not said, and this is not inside a Git checkout. "
            f"Say it with --project or RELAY_PROJECT. Registered: {', '.join(known) or 'none'}"
        )
    addresses = set()
    for address in checkout.remotes:
        if "://" not in address and ":" not in address.split("/", 1)[0]:
            address = str((checkout.root / address).resolve())  # a path, as Git reads it
        try:
            addresses.add(canonical_repository(address))
        except Exception:  # noqa: BLE001, S112 - an address that cannot be used matches nothing
            continue
    here = [
        row["project_id"]
        for row in projects
        if row["repository"] in addresses and row["trunk"] == checkout.branch
    ]
    if len(here) == 1:
        return str(here[0])
    of_this_repository = [
        f"{row['project_id']} (branch {row['trunk']})"
        for row in projects
        if row["repository"] in addresses
    ]
    raise NoProject(
        f"this checkout is on branch {checkout.branch or 'no branch'}, which is the trunk of "
        f"{'no' if not here else 'more than one'} registered project. "
        + (
            f"Of this repository: {', '.join(of_this_repository)}. "
            if of_this_repository
            else "No registered project is of this repository. "
        )
        + "Say which with --project or RELAY_PROJECT, or register this one with `relay init`."
    )
