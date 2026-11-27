"""`relay doctor`: what would stop Relay working here, each with what to do about it.

It reads, and changes nothing. With `--boundary` it also runs the boundary suite for the
bounds nobody has lately seen hold, which is paid for: it says so first, and each run is
a request on record (`boundary_runs`).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import redis.asyncio as redis

from relay.application import authority
from relay.application.boundaries import Boundaries
from relay.application.boundary_runs import Attempt, BoundaryRuns, RunRefused
from relay.application.leadership import installation_of, lock_key
from relay.application.workers import is_alive
from relay.git.checkout import resolve_ref
from relay.persistence.database import Pool, schema_state
from relay.providers.boundary_suite import runtime_now
from relay.providers.claude import _ALWAYS_PASSED, ClaudeAdapter, resolved
from relay.providers.confinement import boundary_digest, confinement_for
from relay.providers.definitions import DEFAULT_DEFINITIONS, find_definition
from relay.runtime import ROLES, settings_of

NEEDED = ("workspaces", "mirrors", "image")
# How long the database is waited for before it is said not to answer.
DATABASE_WITHIN = 8.0


@dataclass(frozen=True)
class Finding:
    what: str
    ok: bool
    saw: str
    # What to do about it, if it is not as it should be.
    do: str = ""


def _runs(*command: str, within: float = 20.0) -> tuple[bool, str]:
    """Whether a command ran and succeeded, and the first line of what it said."""
    try:
        done = subprocess.run(
            list(command), capture_output=True, text=True, timeout=within, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as failed:
        return False, str(failed)
    said = (done.stdout or done.stderr).strip().splitlines()
    return done.returncode == 0, said[0] if said else ""


def boundaries_of(settings: Mapping[str, Any], runtime: Mapping[str, str]) -> dict[str, str]:
    """Each role's boundary on this runtime, by role. Two roles with the same bounds have
    the same one."""
    workspace = Path(settings["workspaces"]) / "run" / "tree"
    return {
        role: boundary_digest(
            confinement_for(
                role, workspace, withheld=[Path(settings["mirrors"])] if role == "builder" else []
            ),
            runtime,
        )
        for role in ROLES
    }


async def examine(pool: Pool, broker: redis.Redis) -> list[Finding]:
    """Look, and change nothing: not a migration, not a row."""
    try:
        pending, drifted = await asyncio.wait_for(schema_state(pool), DATABASE_WITHIN)
    except Exception as unreachable:  # noqa: BLE001 - whatever it is, it is the finding
        return [
            Finding(
                "the database",
                False,
                f"cannot be reached: {unreachable!r}"[:300],
                "start it (`docker compose up -d`) and check RELAY_DATABASE_URL",
            )
        ]
    if drifted:
        return [
            Finding(
                "the database",
                False,
                f"its schema was made by another build of Relay: {', '.join(drifted)} differ",
                "use the build that made it, or a database of this build's own",
            )
        ]
    if pending:
        return [
            Finding(
                "the database",
                False,
                "reached; it has never been set up for Relay"
                if len(pending) > 1 and pending[0].startswith("0001")
                else f"reached; its schema is behind this build by {', '.join(pending)}",
                "run `relay init` in the project's checkout (or `relay up`, which brings an "
                "installation's schema up to date)",
            )
        ]
    found = [Finding("the database", True, "reached, and its schema is current")]
    settings = await settings_of(pool)
    async with pool.connection() as connection:
        installation = await installation_of(connection)

    # Redis, and whether it is the one the control plane uses.
    async with pool.connection() as connection:
        row = await (await connection.execute("SELECT term FROM installation")).fetchone()
        term = int(row["term"]) if row else 0
        led = await (
            await connection.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
                "WHERE d.datname = current_database() AND l.locktype = 'advisory' AND l.granted "
                "AND l.objsubid = 2 AND l.classid = %s::bigint::oid AND l.objid = 1) AS led",
                (lock_key(installation),),
            )
        ).fetchone()
        workers = await (
            await connection.execute("SELECT worker_id, role FROM workers ORDER BY role")
        ).fetchall()
        alive = sorted(
            [str(each["role"]) for each in workers if await is_alive(connection, each["worker_id"])]
        )
        projects = await (
            await connection.execute("SELECT project_id, repository, trunk FROM projects")
        ).fetchall()
    try:
        marked = await broker.get(f"{installation}:control")
        if marked is None:
            found.append(
                Finding(
                    "Redis",
                    term == 0 or not (led and led["led"]),
                    "reached; the control plane has left no mark in it",
                    "if `relay up` is running, it was given another Redis than this: give "
                    "both the same RELAY_REDIS_URL",
                )
            )
        elif int(marked) < term:
            found.append(
                Finding(
                    "Redis",
                    False,
                    f"marked by the control plane of term {int(marked)}; the installation is "
                    f"at term {term}",
                    "the control plane now running uses another Redis: give this the same "
                    "RELAY_REDIS_URL",
                )
            )
        else:
            found.append(Finding("Redis", True, "reached, and it is the control plane's"))
    except (redis.RedisError, OSError) as unreachable:
        found.append(
            Finding(
                "Redis",
                False,
                f"cannot be reached: {unreachable}",
                "start it: `docker compose up -d`",
            )
        )

    # The processes.
    found.append(
        Finding(
            "the control plane",
            bool(led and led["led"]),
            "running" if led and led["led"] else "not running",
            "start it: `relay up`",
        )
    )
    for role in ROLES:
        found.append(
            Finding(
                f"a worker for the {role}",
                role in alive,
                f"{alive.count(role)} running" if role in alive else "none running",
                f"start one: `relay worker start --role {role}`",
            )
        )

    # What the installation has been told.
    missing = [name for name in NEEDED if not settings.get(name)]
    found.append(
        Finding(
            "the installation's settings",
            not missing,
            "set" if not missing else f"not set: {', '.join(missing)}",
            "run `relay init` in the project's checkout",
        )
    )
    found.append(
        Finding(
            "a project",
            bool(projects),
            ", ".join(f"{each['project_id']} ({each['trunk']})" for each in projects) or "none",
            "run `relay init` in the project's checkout",
        )
    )
    found.append(
        Finding(
            "the Product Owner's passphrase",
            await authority.is_set(pool),
            "set" if await authority.is_set(pool) else "not set",
            "run `relay init`",
        )
    )
    policy = await authority.policy_of(pool)
    found.append(
        Finding(
            "the operator's statement of remote policy",
            policy is not None,
            f"revision {policy.revision}, by {policy.stated_by}" if policy else "not made",
            'workers launch nothing without it: `relay policy state "<what applies to this host>"`',
        )
    )

    # What Relay runs.
    git_ok, git_saw = _runs("git", "--version")
    found.append(Finding("Git", git_ok, git_saw, "install Git"))
    docker_ok, docker_saw = _runs("docker", "version", "--format", "{{.Server.Version}}")
    found.append(
        Finding(
            "Docker",
            docker_ok,
            f"server {docker_saw}" if docker_ok else docker_saw or "not answering",
            "start Docker: the oracle observes every red, green and check in a container",
        )
    )
    if settings.get("image"):
        there, _ = _runs("docker", "image", "inspect", str(settings["image"]), "--format", "ok")
        found.append(
            Finding(
                "the oracle's image",
                there,
                str(settings["image"]) if there else f"{settings['image']} is not on this host",
                f"pull it: `docker pull {settings['image']}`",
            )
        )
    for project in projects:
        trunk = f"refs/heads/{project['trunk']}"
        try:
            # The lookup the workflow itself makes: the exact ref, and not whatever
            # else in the repository has a name that ends the same way.
            head = await resolve_ref(str(project["repository"]), trunk, timeout=20.0)
            there, saw = (
                head is not None,
                (f"branch {project['trunk']} is there" if head else f"there is no {trunk}"),
            )
        except Exception as unreachable:  # noqa: BLE001 - whatever it is, it is said
            there, saw = False, str(unreachable)[:200]
        found.append(
            Finding(
                f"the repository of {project['project_id']}",
                there,
                saw,
                "check the address, the branch, and that Git here has access to it",
            )
        )

    # The provider's CLI.
    binary = str(settings.get("cli", "claude"))
    if shutil.which(binary) is None and not Path(binary).is_file():
        found.append(
            Finding(
                "the Claude CLI",
                False,
                f"{binary} is not installed",
                "install Claude Code and sign in",
            )
        )
        return found
    try:
        capabilities = await ClaudeAdapter(binary).capabilities()
        lacking = [
            flag for flag in (*_ALWAYS_PASSED, "--max-budget-usd") if flag not in capabilities.flags
        ]
        found.append(
            Finding(
                "the Claude CLI",
                not lacking,
                f"{capabilities.version} at {resolved(binary)}"
                if not lacking
                else f"{capabilities.version} does not offer {', '.join(lacking)}",
                "update Claude Code: Relay asks for nothing it cannot be sure was asked for",
            )
        )
    except Exception as unusable:  # noqa: BLE001 - whatever it is, it is said
        found.append(
            Finding("the Claude CLI", False, str(unusable), "install Claude Code and sign in")
        )
        return found
    directories = [Path(each) for each in settings.get("definitions", [])]
    for role in ROLES:
        try:
            definition = find_definition(DEFAULT_DEFINITIONS[role], directories)
            found.append(Finding(f"the {role}'s definition", True, str(definition.path)))
        except LookupError:
            found.append(
                Finding(
                    f"the {role}'s definition",
                    False,
                    f"{DEFAULT_DEFINITIONS[role]}.md is in none of the places searched",
                    f"install it in one of: {', '.join(map(str, directories)) or '(none set)'}",
                )
            )

    # The bounds of a run, seen to hold on this runtime.
    if missing or policy is None:
        return found
    runtime = runtime_now(policy.statement, binary)
    boundaries = Boundaries(pool)
    for role, digest in boundaries_of(settings, runtime).items():
        async with pool.connection() as connection:
            last = await (
                await connection.execute(
                    "SELECT passed, observed_at, now() - observed_at AS age FROM boundary_passes "
                    "WHERE digest = %s ORDER BY observed_at DESC, pass_id DESC LIMIT 1",
                    (digest,),
                )
            ).fetchone()
        holds = await boundaries.holds(digest)
        if last is None:
            saw = "never seen to hold on this runtime"
        elif not last["passed"]:
            saw = f"did not hold when last tried, {_ago(last['age'])} ago"
        else:
            saw = f"held {_ago(last['age'])} ago" + ("" if holds else ", which is too long ago")
        found.append(
            Finding(
                f"the bounds of a {role}",
                holds,
                saw,
                "run the suite, which makes paid calls: `relay doctor --boundary`",
            )
        )
    return found


def _ago(age: Any) -> str:
    seconds = int(age.total_seconds())
    if seconds < 120:
        return f"{seconds} seconds"
    if seconds < 7200:
        return f"{seconds // 60} minutes"
    return f"{seconds // 3600} hours"


def report(found: list[Finding]) -> str:
    lines = []
    for each in found:
        lines.append(f"{'ok ' if each.ok else 'NOT'}  {each.what}: {each.saw}")
        if not each.ok and each.do:
            lines.append(f"       {each.do}")
    wrong = sum(1 for each in found if not each.ok)
    lines.append("")
    lines.append("nothing stands in the way" if not wrong else f"{wrong} to put right")
    return "\n".join(lines)


SUITE_MODEL = "claude-haiku-5-5"
A_RUN_COSTS_AT_MOST = "0.25"


async def owed_runs(
    pool: Pool, settings: Mapping[str, Any], runtime: Mapping[str, str]
) -> dict[str, str]:
    """The roles whose bounds nobody has lately seen hold on this runtime, with their
    boundary: one role for each boundary, since it is the bounds that are tried."""
    boundaries, owed, seen = Boundaries(pool), {}, set()
    for role, digest in boundaries_of(settings, runtime).items():
        if digest in seen or await boundaries.holds(digest):
            continue
        seen.add(digest)
        owed[role] = digest
    return owed


async def run_boundaries(
    pool: Pool,
    runs: BoundaryRuns,
    settings: Mapping[str, Any],
    runtime: Mapping[str, str],
    request_id: str,
    installation: str,
    retry: uuid.UUID | None = None,
    before_launching: Callable[[list[str]], None] | None = None,
) -> list[Attempt | RunRefused]:
    """Make the runs this request asks for. The request is put on record first, with the
    whole of what it asks to have tried, which may be nothing: every boundary with no
    fresh pass, or the one retry it names. Sent again, it asks for what it asked for
    then. `before_launching` is told which roles may still be bought a run, before any
    is: that is where the cost is said."""

    async def work_out() -> Mapping[str, str]:
        if retry is None:
            return await owed_runs(pool, settings, runtime)
        async with pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT role FROM boundary_runs WHERE attempt_id = %s", (retry,)
                )
            ).fetchone()
        if row is None:
            raise RunRefused(f"there is no attempt {retry} to retry")
        # The bounds as they are on the runtime about to be exercised, not as they were
        # when the attempt was made: that is what the run will be a run of.
        role = str(row["role"])
        return {role: boundaries_of(settings, runtime)[role]}

    try:
        asked = await runs.open_request(request_id, installation, retry, work_out)
    except RunRefused as refused:
        return [refused]
    buying = await runs.to_launch(request_id, asked)
    if buying and before_launching is not None:
        before_launching(buying)
    made: list[Attempt | RunRefused] = []
    today = boundaries_of(settings, runtime)
    for role, digest in asked.items():
        try:
            made.append(
                await runs.run(
                    request_id, role, digest, runtime, retry=retry, exercising=today.get(role)
                )
            )
        except RunRefused as refused:
            made.append(refused)
    return made
