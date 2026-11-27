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

from relay.application import authority
from relay.cli.selection import Checkout, checkout_at
from relay.git.checkout import canonical_repository
from relay.persistence.database import Pool, migrate
from relay.runtime import assemble, configure, settings_of, wake

DEFAULT_IMAGE = "python:3.14-alpine"


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
    """Do what is not yet done, and say what was done and what was found already so."""
    said = []
    await migrate(pool)
    async with pool.connection() as connection:
        row = await (
            await connection.execute("SELECT installation_id::text AS id FROM installation")
        ).fetchone()
    assert row is not None
    installation = row["id"]

    # Worked out before anything is changed: an enrolment that cannot name its project
    # leaves no half of itself behind.
    checkout = checkout_at(directory)
    repository, trunk = _source(asked, checkout)

    # 1. The installation's settings: each is set once.
    settings = await settings_of(pool)
    root = default_root(installation)
    wanted = {
        "workspaces": asked.workspaces or str(root / "workspaces"),
        "mirrors": asked.mirrors or str(root / "mirrors"),
    }
    setting: dict[str, object] = {}
    for name, value in wanted.items():
        if settings.get(name):
            if getattr(asked, name) and settings[name] != str(Path(value).resolve()):
                said.append(f"{name}: left at {settings[name]} (already set; not changed by init)")
            continue
        place = Path(value).resolve()
        if place.is_relative_to(Path.home()):
            raise CannotEnrol(
                f"{name} cannot be under the home directory, which is hidden from every run: "
                f"{place}"
            )
        place.mkdir(parents=True, exist_ok=True, mode=0o700)
        setting[name] = str(place)
        said.append(f"{name}: {place}")
    if not settings.get("image"):
        setting["image"] = pin(asked.image or DEFAULT_IMAGE)
        said.append(f"the oracle's image: {setting['image']}")
    elif asked.image:
        said.append(f"the oracle's image: left at {settings['image']} (already set)")
    if "definitions" not in settings:
        setting["definitions"] = asked.definitions or [str(Path.home() / ".claude" / "agents")]
        said.append(f"agent definitions are looked for in: {', '.join(setting['definitions'])}")  # type: ignore[arg-type]
    if setting:
        await configure(pool, **setting)
    else:
        said.append(f"installation {installation}: already set up")

    # 2. The project: this checkout's, unless said otherwise.
    async with pool.connection() as connection:
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
        project = asked.project or _identifier_from(
            (checkout.root if checkout else Path(repository)).name
        )
        relay = await assemble(pool, broker)
        try:
            await relay.behaviours.open_project(project, repository, trunk)
        except ValueError as refused:
            raise CannotEnrol(str(refused)) from refused
        await wake(pool, installation, project)
        said.append(f"project {project}: registered, branch {trunk} of {repository}")

    # 3. The Product Owner's passphrase: set once, by a person at the terminal.
    if await authority.is_set(pool):
        said.append("the passphrase: already set (change it with `relay passphrase`)")
    else:
        first = read_secret(
            f"Set the Product Owner's passphrase (at least {authority.SHORTEST} characters): "
        )
        if first != read_secret("Again: "):
            raise CannotEnrol(
                "the two did not match: no passphrase was set. Run `relay init` again"
            )
        try:
            was_set = await authority.set_first(pool, first)
        except authority.Refused as refused:
            raise CannotEnrol(
                f"{refused}: no passphrase was set. Run `relay init` again"
            ) from refused
        said.append(
            "the passphrase: set" if was_set else "the passphrase: set by someone else meanwhile"
        )
    return said


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
