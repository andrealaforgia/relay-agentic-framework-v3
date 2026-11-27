"""`relay doctor`: what stands in the way, and what to do about each thing."""

from __future__ import annotations

import contextvars
import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import redis.asyncio as redis
from psycopg_pool import AsyncConnectionPool

from relay.application import authority
from relay.application.boundaries import Boundaries
from relay.application.boundary_runs import Attempt, BoundaryRuns
from relay.application.leadership import Leadership, installation_of
from relay.cli import doctor
from relay.cli.enrolment import Asked, enrol
from relay.git.checkout import name_attempts_under
from relay.providers.boundary_suite import Observation, SuiteResult, runtime_now
from relay.providers.definitions import DEFAULT_DEFINITIONS
from relay.runtime import ROLES, configure, settings_of
from tests.integration.person import with_no_terminal
from tests.integration.test_enrolment import IMAGE, PASSPHRASE, Person, checkout_of, git
from tests.integration.test_processes import ACTING


@pytest.fixture(autouse=True)
def unnamed_afterwards() -> Any:
    yield
    name_attempts_under("")


@pytest.fixture
async def enrolled(
    database: AsyncConnectionPool, broker: redis.Redis, tmp_path: Path
) -> AsyncConnectionPool:
    """An installation as `relay init` leaves it, with the stand-in CLI as its CLI."""
    root = tmp_path.resolve()
    here = checkout_of(tmp_path / "remote.git", tmp_path / "vat")
    (here / "vat.py").write_text("print(1)\n")
    git(here, "add", "-A")
    git(here, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x")
    git(here, "push", "-q", "origin", "main")
    definitions = root / "definitions"
    definitions.mkdir()
    for name in set(DEFAULT_DEFINITIONS.values()):
        (definitions / f"{name}.md").write_text(f"---\nname: {name}\n---\nDo the work.\n")
    cli = root / "cli" / "claude"
    cli.parent.mkdir()
    shutil.copy(ACTING, cli)
    asked = Asked(
        workspaces=str(root / "w"),
        mirrors=str(root / "m"),
        image=IMAGE,
        definitions=[str(definitions)],
    )
    await enrol(database, broker, here, asked, Person(PASSPHRASE, PASSPHRASE), pin=str)
    await configure(database, cli=str(cli))
    return database


async def examined(database: AsyncConnectionPool, broker: redis.Redis) -> dict[str, doctor.Finding]:
    async with database.connection() as connection:
        installation = await installation_of(connection)
    return {each.what: each for each in await doctor.examine(database, broker, installation)}


def passing(settings: Mapping[str, Any], launched: list[str]) -> Any:
    async def suite(role: str, runtime: Mapping[str, str]) -> SuiteResult:
        launched.append(role)
        digest = doctor.boundaries_of(settings, runtime)[role]
        return SuiteResult(role, digest, dict(runtime), (Observation("held", True, ""),), None)

    return suite


async def test_doctor_names_each_thing_that_stands_in_the_way_and_what_to_do(
    enrolled: AsyncConnectionPool, broker: redis.Redis
) -> None:
    found = await examined(enrolled, broker)

    assert found["the database"].ok and found["Git"].ok and found["Docker"].ok
    assert found["the installation's settings"].ok and found["a project"].ok
    assert found["the Product Owner's passphrase"].ok
    assert found["the repository of vat"].ok
    assert found["the Claude CLI"].ok and "9.9.9" in found["the Claude CLI"].saw
    assert all(found[f"the {role}'s definition"].ok for role in ROLES)
    # Not running, not stated, not there: each said with the command that puts it right.
    assert not found["the control plane"].ok and "relay up" in found["the control plane"].do
    assert "relay worker start --role qa" in found["a worker for the qa"].do
    policy = found["the operator's statement of remote policy"]
    assert not policy.ok and "relay policy state" in policy.do
    assert not found["the oracle's image"].ok and "docker pull" in found["the oracle's image"].do
    assert "the bounds of a qa" not in found  # nothing to try them against until it is stated
    assert "to put right" in doctor.report(list(found.values()))


async def test_doctor_says_whose_bounds_have_not_been_seen_to_hold_and_a_run_puts_that_right(
    enrolled: AsyncConnectionPool, broker: redis.Redis
) -> None:
    """One run for each boundary, not for each role; a request sent again runs nothing
    and returns what it did; and with every boundary fresh nothing is owed."""
    policy = await authority.state_policy(enrolled, PASSPHRASE, "none applies here", "andrea")
    settings = await settings_of(enrolled)
    runtime = runtime_now(policy.statement, str(settings["cli"]))
    found = await examined(enrolled, broker)
    assert all(not found[f"the bounds of a {role}"].ok for role in ROLES)
    assert "relay doctor --boundary" in found["the bounds of a qa"].do
    assert "never seen to hold" in found["the bounds of a qa"].saw

    launched: list[str] = []
    runs = BoundaryRuns(enrolled, Boundaries(enrolled), passing(settings, launched))
    owed = await doctor.owed_runs(enrolled, settings, runtime)
    assert len(set(owed.values())) == len(owed) <= len(ROLES)

    made = await doctor.run_boundaries(enrolled, runs, settings, runtime, "request-1")

    assert [each.state for each in made if isinstance(each, Attempt)] == ["passed"] * len(owed)
    assert launched == list(owed)
    found = await examined(enrolled, broker)
    assert all(found[f"the bounds of a {role}"].ok for role in ROLES)
    assert await doctor.owed_runs(enrolled, settings, runtime) == {}
    again = await doctor.run_boundaries(enrolled, runs, settings, runtime, "request-1")
    assert [
        (each.attempt_id, each.launched_now) for each in again if isinstance(each, Attempt)
    ] == [(each.attempt_id, False) for each in made if isinstance(each, Attempt)]
    assert await doctor.run_boundaries(enrolled, runs, settings, runtime, "request-2") == []
    assert launched == list(owed)


async def test_doctor_names_a_redis_that_is_not_the_control_planes(
    enrolled: AsyncConnectionPool, broker: redis.Redis
) -> None:
    """3C-2.4: the marker of an earlier term than the installation is at."""

    async def led() -> int:
        mine = await Leadership.take(enrolled)
        await mine.release()
        return mine.term

    async def as_a_process() -> int:
        import asyncio

        return await asyncio.create_task(led(), context=contextvars.copy_context())

    async with enrolled.connection() as connection:
        installation = await installation_of(connection)
    try:
        await broker.set(f"{installation}:control", await as_a_process())
        assert (await examined(enrolled, broker))["Redis"].ok
        await as_a_process()
        mismatched = (await examined(enrolled, broker))["Redis"]
        assert not mismatched.ok and "another Redis" in mismatched.do
    finally:
        await broker.delete(f"{installation}:control")


async def test_relay_doctor_is_a_command_that_needs_no_terminal_and_exits_by_what_it_found(
    enrolled: AsyncConnectionPool,
    shell: dict[str, str],
    tmp_path: Path,
) -> None:
    here = tmp_path / "vat"

    said = with_no_terminal(["doctor"], shell, here)
    as_json = with_no_terminal(["doctor", "--json"], shell, here)
    untried = with_no_terminal(["doctor", "--boundary"], shell, here)

    assert said.code == 1
    assert "ok   the database" in said.printed and "NOT  the control plane" in said.printed
    facts = {each["what"]: each for each in json.loads(as_json.printed)}
    assert facts["the control plane"] == {
        "what": "the control plane",
        "ok": False,
        "saw": "not running",
        "do": "start it: `relay up`",
    }
    assert untried.code == 1 and "until the installation is set up and the policy stated" in (
        untried.printed
    )
