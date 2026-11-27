"""`relay init`, and which project a command means."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import redis.asyncio as redis
from psycopg_pool import AsyncConnectionPool

from relay.application import authority
from relay.cli.enrolment import Asked, CannotEnrol, enrol
from relay.cli.selection import NoProject, select_project
from relay.git.checkout import name_attempts_under
from relay.runtime import settings_of

PASSPHRASE = "correct horse battery"
IMAGE = "python@sha256:" + "0" * 64


def git(at: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(at), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def checkout_of(remote: Path, at: Path, branch: str = "main") -> Path:
    """A checkout with one remote, on a branch."""
    if not remote.exists():
        remote.mkdir(parents=True)
        git(remote, "init", "-q", "--bare", "-b", "main")
    at.mkdir(parents=True)
    git(at, "init", "-q", "-b", branch)
    git(at, "remote", "add", "origin", str(remote))
    return at


class Person:
    """Types what the test says, when asked at the terminal."""

    def __init__(self, *typed: str) -> None:
        self.typed, self.asked = list(typed), []

    def __call__(self, prompt: str) -> str:
        self.asked.append(prompt)
        return self.typed.pop(0)


@pytest.fixture
def places(tmp_path: Path) -> Asked:
    root = tmp_path.resolve()
    return Asked(workspaces=str(root / "w"), mirrors=str(root / "m"), image=IMAGE)


@pytest.fixture(autouse=True)
def unnamed_afterwards() -> Any:
    yield
    name_attempts_under("")


async def test_init_sets_the_installation_up_registers_the_checkouts_project_and_sets_the_passphrase(
    database: AsyncConnectionPool, broker: redis.Redis, tmp_path: Path, places: Asked
) -> None:
    here = checkout_of(tmp_path / "remote.git", tmp_path / "vat-calculator")
    person = Person(PASSPHRASE, PASSPHRASE)

    said = await enrol(database, broker, here, places, person, pin=lambda image: image)

    settings = await settings_of(database)
    assert settings["workspaces"] == places.workspaces and Path(places.workspaces).is_dir()
    assert settings["mirrors"] == places.mirrors and settings["image"] == IMAGE
    async with database.connection() as connection:
        projects = await (
            await connection.execute("SELECT project_id, repository, trunk FROM projects")
        ).fetchall()
    assert [(each["project_id"], each["trunk"]) for each in projects] == [
        ("vat-calculator", "main")
    ]
    assert projects[0]["repository"] == str((tmp_path / "remote.git").resolve())
    await authority.require(database, PASSPHRASE)
    assert len(person.asked) == 2
    assert any("project vat-calculator: registered" in line for line in said)
    assert await select_project(database, None, {}, here) == "vat-calculator"


async def test_init_run_again_finds_what_is_there_and_leaves_it_alone(
    database: AsyncConnectionPool, broker: redis.Redis, tmp_path: Path, places: Asked
) -> None:
    """3C-1.5: a second enrolment changes neither the settings nor the passphrase, and
    asks nobody for one."""
    here = checkout_of(tmp_path / "remote.git", tmp_path / "vat")
    await enrol(database, broker, here, places, Person(PASSPHRASE, PASSPHRASE), pin=str)
    before = await settings_of(database)
    nobody = Person()
    elsewhere = Asked(
        workspaces=str(tmp_path.resolve() / "other"), image="python@sha256:" + "1" * 64
    )

    said = await enrol(database, broker, here, elsewhere, nobody, pin=str)

    assert await settings_of(database) == before
    assert nobody.asked == []
    await authority.require(database, PASSPHRASE)
    assert any("project vat: already registered" in line for line in said)
    assert any("workspaces: left at" in line for line in said)
    assert any("the passphrase: already set" in line for line in said)


async def test_a_second_project_is_registered_and_changes_nothing_of_the_installation(
    database: AsyncConnectionPool, broker: redis.Redis, tmp_path: Path, places: Asked
) -> None:
    """3C-1.4: what is the installation's is the same for every project."""
    first = checkout_of(tmp_path / "a.git", tmp_path / "a")
    second = checkout_of(tmp_path / "b.git", tmp_path / "b")
    await enrol(database, broker, first, places, Person(PASSPHRASE, PASSPHRASE), pin=str)
    before = await settings_of(database)

    await enrol(database, broker, second, Asked(project="second"), Person(), pin=str)

    assert await settings_of(database) == before
    assert await select_project(database, None, {}, first) == "a"
    assert await select_project(database, None, {}, second) == "second"


async def test_two_passphrases_that_do_not_match_set_none(
    database: AsyncConnectionPool, broker: redis.Redis, tmp_path: Path, places: Asked
) -> None:
    here = checkout_of(tmp_path / "remote.git", tmp_path / "vat")

    with pytest.raises(CannotEnrol, match="did not match"):
        await enrol(database, broker, here, places, Person(PASSPHRASE, "something else"), pin=str)

    assert not await authority.is_set(database)


async def test_init_says_what_it_cannot_work_out(
    database: AsyncConnectionPool, broker: redis.Redis, tmp_path: Path, places: Asked
) -> None:
    with pytest.raises(CannotEnrol, match="not inside a Git checkout"):
        await enrol(database, broker, tmp_path, places, Person(), pin=str)
    assert await settings_of(database) == {}  # and nothing of it was done
    one = checkout_of(tmp_path / "one.git", tmp_path / "one")
    under_home = Asked(workspaces=str(Path.home() / "relay-workspaces"), image=IMAGE)
    with pytest.raises(CannotEnrol, match="home directory"):
        await enrol(database, broker, one, under_home, Person(), pin=str)
    two = checkout_of(tmp_path / "remote.git", tmp_path / "two")
    git(two, "remote", "add", "fork", str(tmp_path / "fork.git"))
    with pytest.raises(CannotEnrol, match="2 remotes"):
        await enrol(database, broker, two, places, Person(), pin=str)


async def test_which_project_a_command_means_is_said_or_is_this_checkouts_and_is_never_guessed(
    database: AsyncConnectionPool, broker: redis.Redis, tmp_path: Path, places: Asked
) -> None:
    """3C-2.6: by --project, then RELAY_PROJECT, then the checkout the command is run in
    if its remote and branch are one project's. Anything else is refused by name."""
    on_main = checkout_of(tmp_path / "remote.git", tmp_path / "main-checkout")
    on_next = checkout_of(tmp_path / "remote.git", tmp_path / "next-checkout", branch="next")
    await enrol(database, broker, on_main, places, Person(PASSPHRASE, PASSPHRASE), pin=str)
    await enrol(database, broker, on_next, Asked(project="next-release"), Person(), pin=str)
    inside = on_next / "src" / "deep"
    inside.mkdir(parents=True)

    assert await select_project(database, None, {}, on_main) == "main-checkout"
    assert await select_project(database, None, {}, inside) == "next-release"
    assert await select_project(database, "main-checkout", {}, inside) == "main-checkout"
    assert (
        await select_project(database, None, {"RELAY_PROJECT": "main-checkout"}, inside)
        == "main-checkout"
    )
    assert (
        await select_project(database, "next-release", {"RELAY_PROJECT": "main-checkout"}, on_main)
        == "next-release"
    )

    with pytest.raises(NoProject, match="not a registered project. Registered: main-checkout, "):
        await select_project(database, "nonesuch", {}, on_main)
    with pytest.raises(NoProject, match="not inside a Git checkout"):
        await select_project(database, None, {}, tmp_path)
    git(on_main, "checkout", "-q", "-b", "feature")
    with pytest.raises(
        NoProject, match=r"main-checkout \(branch main\), next-release \(branch next\)"
    ):
        await select_project(database, None, {}, on_main)
    stranger = checkout_of(tmp_path / "unknown.git", tmp_path / "stranger")
    with pytest.raises(NoProject, match="No registered project is of this repository"):
        await select_project(database, None, {}, stranger)
