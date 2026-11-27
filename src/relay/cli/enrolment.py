"""`relay init`: the installation is told what it needs, a project is registered, and the
Product Owner's passphrase is set. Each of the three is done once: run again, it finds
what is there and leaves it alone, and says so."""

from __future__ import annotations

import os
import platform
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as redis
from psycopg.types.json import Jsonb

from relay.application import authority
from relay.application.behaviours import open_project_in
from relay.cli.selection import Checkout, checkout_at
from relay.git.checkout import canonical_repository
from relay.persistence.database import Pool, migrate
from relay.providers.confinement import confinement_for
from relay.runtime import wake

DEFAULT_IMAGE = "python:3.14-alpine"
_PINNED = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")


class CannotEnrol(Exception):
    """Something `relay init` needs is missing, and it says what."""


@dataclass
class Asked:
    """What the person said on the command line; what they did not say is worked out."""

    project: str | None = None
    repository: str | None = None
    trunk: str | None = None
    workspaces: str | None = None
    mirrors: str | None = None
    image: str | None = None
    definitions: list[str] = field(default_factory=list)


def default_root(installation: str) -> Path:
    """Where an installation keeps its workspaces and mirrors when not told: outside the
    home directory, which is hidden from every run, and outside the temporary directory
    every sandboxed shell shares."""
    if platform.system() == "Darwin":
        return Path("/Users/Shared/relay") / installation
    return Path("/var/tmp") / f"relay-{os.getuid()}" / installation


def pinned(image: str) -> str:
    """The image by its digest: what the oracle runs must not change under its name."""
    if "@sha256:" in image:
        return image
    pulled = subprocess.run(
        ["docker", "pull", "-q", image],
        capture_output=True,
        text=True,
        check=False,
    )
    if pulled.returncode != 0:
        raise CannotEnrol(
            f"the oracle's image {image} could not be pulled, so it cannot be pinned by its "
            f"digest: {pulled.stderr.strip() or 'is Docker running?'}"
        )
    digest = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{index .RepoDigests 0}}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if "@sha256:" not in digest:
        raise CannotEnrol(f"Docker gives no digest for {image}")
    return digest


def _identifier_from(name: str) -> str:
    made = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return made[:64] or "project"


async def enrol(
    pool: Pool,
    broker: redis.Redis,
    directory: Path,
    asked: Asked,
    read_secret: Callable[[str], str],
    pin: Callable[[str], str] = pinned,
) -> list[str]:
    """Do what is not yet done, and say what was done and what was found already so.

    Everything is gathered and checked first: where the project comes from, where the
    installation would keep its work, the image, and the passphrase from the person.
    Then it is decided in one transaction, under the installation's lock: what the
    installation has not been told, the first passphrase, and the project. An enrolment
    that is refused, at the terminal or anywhere before, leaves nothing of itself; two
    at once are one after the other, and the second finds the first's."""
    said: list[str] = []
    await migrate(pool)
    async with pool.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT installation_id::text AS id, settings, passphrase IS NOT NULL AS has "
                "FROM installation"
            )
        ).fetchone()
    assert row is not None
    installation, at_first_look = row["id"], dict(row["settings"])

    checkout = checkout_at(directory)
    repository, trunk = _source(asked, checkout)
    proposed = _places(asked, installation, at_first_look)
    if not at_first_look.get("image"):
        image = pin(asked.image or DEFAULT_IMAGE)
        if not _PINNED.fullmatch(image):
            raise CannotEnrol(
                f"the oracle's image is named by its whole digest, and {image!r} is not: "
                "name@sha256: followed by 64 hexadecimal digits. Nothing was set up"
            )
        proposed["image"] = image
    first: str | None = None
    if not row["has"]:
        first = read_secret(
            f"Set the Product Owner's passphrase (at least {authority.SHORTEST} characters): "
        )
        if first != read_secret("Again: "):
            raise CannotEnrol("the two did not match: nothing was set up. Run `relay init` again")
        try:
            authority.require_usable(first)
        except authority.Refused as refused:
            raise CannotEnrol(f"{refused}: nothing was set up. Run `relay init` again") from refused
    for name in ("workspaces", "mirrors"):
        if name in proposed:
            Path(str(proposed[name])).mkdir(parents=True, exist_ok=True, mode=0o700)

    registered: str | None = None
    async with pool.connection() as connection, connection.transaction():
        held = await (
            await connection.execute("SELECT settings, passphrase FROM installation FOR UPDATE")
        ).fetchone()
        assert held is not None
        # What another enrolment set while this one was gathering is kept as it set it.
        setting = {
            name: value for name, value in proposed.items() if not held["settings"].get(name)
        }
        if setting:
            await connection.execute(
                "UPDATE installation SET settings = settings || %s", (Jsonb(setting),)
            )
        for name in ("workspaces", "mirrors", "image", "definitions"):
            if name in setting:
                shown = setting[name]
                said.append(f"{name}: {', '.join(shown) if isinstance(shown, list) else shown}")
            elif getattr(asked, name):
                said.append(
                    f"{name}: left at {held['settings'].get(name)} (already set; not changed "
                    "by init)"
                )
        if not setting:
            said.append(f"installation {installation}: already set up")
        if held["passphrase"] is None and first is not None:
            await connection.execute(
                "UPDATE installation SET passphrase = %s", (authority.hashed(first),)
            )
            said.append("the passphrase: set")
        elif first is not None:
            said.append("the passphrase: set by someone else meanwhile, and left as they set it")
        else:
            said.append("the passphrase: already set (change it with `relay passphrase`)")
        there = await (
            await connection.execute(
                "SELECT project_id FROM projects WHERE repository = %s AND trunk = %s",
                (repository, trunk),
            )
        ).fetchone()
        if there is not None:
            if asked.project and asked.project != there["project_id"]:
                raise CannotEnrol(
                    f"branch {trunk} of this repository is already registered, as "
                    f"{there['project_id']}: it cannot also be {asked.project}"
                )
            said.append(f"project {there['project_id']}: already registered (branch {trunk})")
        else:
            registered = asked.project or _identifier_from(
                (checkout.root if checkout else Path(repository)).name
            )
            try:
                await open_project_in(connection, registered, repository, trunk)
            except ValueError as refused:
                raise CannotEnrol(str(refused)) from refused
            said.append(f"project {registered}: registered, branch {trunk} of {repository}")
    if registered is not None:
        await wake(pool, installation, registered)
    return said


def _places(asked: Asked, installation: str, settings: dict[str, object]) -> dict[str, object]:
    """Where the installation would keep its work and look for definitions, for whatever
    it has not been told: each checked as a worker will check it, before anything is
    set, so that nothing is set that a worker would then refuse."""
    root = default_root(installation)
    proposed: dict[str, object] = {}
    for name, said in (("workspaces", asked.workspaces), ("mirrors", asked.mirrors)):
        if settings.get(name):
            continue
        place = Path(said or str(root / name)).resolve()
        if place.is_relative_to(Path.home()):
            raise CannotEnrol(
                f"{name} cannot be under the home directory, which is hidden from every run: "
                f"{place}"
            )
        proposed[name] = str(place)
    if "workspaces" in proposed:
        mirrors = Path(str(proposed.get("mirrors") or settings.get("mirrors")))
        try:
            # The rule a worker applies to every workspace it is about to run in.
            confinement_for(
                "builder", Path(str(proposed["workspaces"])) / "run" / "tree", withheld=[mirrors]
            )
        except ValueError as unusable:
            raise CannotEnrol(
                f"workspaces cannot be at {proposed['workspaces']}: {unusable}"
            ) from unusable
    if "definitions" not in settings:
        # Resolved now: a worker started from another directory looks in the same places.
        proposed["definitions"] = [
            str(Path(each).expanduser().resolve())
            for each in (asked.definitions or [str(Path.home() / ".claude" / "agents")])
        ]
    return proposed


def _source(asked: Asked, checkout: Checkout | None) -> tuple[str, str]:
    """The repository address and the trunk a project is registered with (D-9)."""
    repository = asked.repository
    if repository is None:
        if checkout is None:
            raise CannotEnrol(
                "this is not inside a Git checkout: run `relay init` in the project's "
                "checkout, or say --repository and --trunk"
            )
        if len(checkout.remotes) != 1:
            raise CannotEnrol(
                f"this checkout has {len(checkout.remotes)} remotes: say which address is "
                "the project's with --repository"
            )
        repository = checkout.remotes[0]
        if "://" not in repository and ":" not in repository.split("/", 1)[0]:
            repository = str((checkout.root / repository).resolve())  # a path, as Git reads it
    trunk = asked.trunk or (checkout.branch if checkout else None)
    if not trunk:
        raise CannotEnrol("which branch is the trunk could not be told: say --trunk")
    try:
        return canonical_repository(repository), trunk
    except Exception as refused:
        raise CannotEnrol(f"the repository's address cannot be used: {refused}") from refused
