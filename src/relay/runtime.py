"""What a Relay process is made of, and where it finds that out.

A process is told two things from outside: where the database is, and where Redis is.
Everything else is the installation's, a row in that database, the same for every
process and every project whichever directory it was started in.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import redis.asyncio as redis
from psycopg.types.json import Jsonb

from relay.application.assignments import Assignments
from relay.application.authoring import (
    AssessTask,
    ReconfirmTask,
    RoadmapTask,
    SealTask,
    SpecifyTask,
)
from relay.application.behaviours import Behaviours
from relay.application.boundaries import Boundaries
from relay.application.decisions import Decisions
from relay.application.features import Features
from relay.application.integration import Integration
from relay.application.invocations import Invocations
from relay.application.leadership import installation_of
from relay.application.reviews import Reviews
from relay.application.stories import Stories
from relay.application.tasks import BuildTask, Profile, ReviewTask, Tasks, Workspaces
from relay.git.checkout import name_attempts_under
from relay.persistence.database import Pool
from relay.testing.oracle import ContainerIsolation, ProcessOracle
from relay.transport.streams import Streams

ROLES = ("analyst", "verifier", "builder", "qa", "sentinel")


class NotConfigured(Exception):
    """The process was not told where the database or Redis is, or the installation has
    not been told what it needs."""


@dataclass(frozen=True)
class Bootstrap:
    """All a process is told from outside the database."""

    database: str
    redis: str
    # A schema of the database to work in; the default one if not given.
    schema: str | None = None


def bootstrap(environ: Mapping[str, str] = os.environ) -> Bootstrap:
    """From the environment, or from the file `RELAY_CONFIG` names, or from the user's
    own configuration directory. Never from the working directory or a repository: what
    a process serves is not decided by where it was started."""
    said = {
        "database": environ.get("RELAY_DATABASE_URL"),
        "redis": environ.get("RELAY_REDIS_URL"),
        "schema": environ.get("RELAY_DATABASE_SCHEMA"),
    }
    named = environ.get("RELAY_CONFIG")
    path = Path(named) if named else Path(environ.get("HOME", "~")) / ".config/relay/relay.toml"
    if path.is_file():
        written = tomllib.loads(path.read_text()).get("relay", {})
        said = {name: value or written.get(name) for name, value in said.items()}
    elif named:
        raise NotConfigured(f"RELAY_CONFIG names {named}, which is not there")
    if not said["database"] or not said["redis"]:
        raise NotConfigured(
            "where the database and Redis are has not been said: set RELAY_DATABASE_URL and "
            f"RELAY_REDIS_URL, or write them under [relay] in {path}"
        )
    return Bootstrap(str(said["database"]), str(said["redis"]), said["schema"])


async def settings_of(pool: Pool) -> dict[str, Any]:
    async with pool.connection() as connection:
        row = await (await connection.execute("SELECT settings FROM installation")).fetchone()
    assert row is not None
    return dict(row["settings"])


async def configure(pool: Pool, **settings: Any) -> None:
    """Say what the installation is to use: merged into what it has."""
    async with pool.connection() as connection:
        await connection.execute(
            "UPDATE installation SET settings = settings || %s", (Jsonb(settings),)
        )


# The most one call may cost when the installation's settings do not say: no call is
# ever launched without a cap.
DEFAULT_BUDGET_USD = Decimal("2.00")


def profile_of(settings: Mapping[str, Any], role: str) -> Profile:
    profiles = settings.get("profiles", {})
    said = {**profiles.get("default", {}), **profiles.get(role, {})}
    budget = Decimal(str(said.get("budget_usd", DEFAULT_BUDGET_USD)))
    if not budget.is_finite() or budget <= 0:
        raise NotConfigured(
            f"the most one call of the {role} may cost is set to {said.get('budget_usd')!r}: "
            "it must be an amount above nothing"
        )
    return Profile(
        model=str(said.get("model", "claude-sonnet-5-5")),
        effort=said.get("effort"),
        timeout=float(said.get("timeout", 1800.0)),
        budget_usd=budget,
    )


async def stated_policy(connection: Any) -> str | None:
    """What the operator has stated of this host's remote policy (D-10), as the
    installation has it in this transaction."""
    row = await (
        await connection.execute("SELECT policy->>'statement' AS statement FROM installation")
    ).fetchone()
    return row["statement"] if row else None


def channel(installation: str) -> str:
    return "relay_" + installation.replace("-", "")


async def wake(pool: Pool, installation: str, project_id: str) -> None:
    """Tell the control plane there may be something for it to do for a project. Sent
    after whatever made that so has committed. Nothing depends on its arriving: the
    control plane looks at every project on a timer as well."""
    async with pool.connection() as connection:
        await connection.execute("SELECT pg_notify(%s, %s)", (channel(installation), project_id))


class Elsewhere(Features):
    """The pass, as every process but the control plane has it: a wake sent to the one
    process that runs it. A worker has no oracle and no access to publish, and a command
    is gone before a pass would end."""

    installation = ""

    async def reconcile(self, project_id: str, *, starting: bool = False) -> None:
        await wake(self._pool, self.installation, project_id)

    async def complete_what_is_owed(self, project_id: str) -> None:
        await wake(self._pool, self.installation, project_id)

    async def start(self) -> None:
        return None


@dataclass
class Relay:
    """The services of one installation, as any of its processes holds them."""

    pool: Pool
    installation: str
    settings: dict[str, Any]
    streams: Streams
    claims: Assignments
    stories: Stories
    behaviours: Behaviours
    reviews: Reviews
    integration: Integration
    invocations: Invocations
    features: Features
    decisions: Decisions
    boundaries: Boundaries
    tasks: dict[str, dict[str, Any]]


async def assemble(pool: Pool, broker: redis.Redis, *, control_plane: bool = False) -> Relay:
    """Everything, from the installation's own record. Refs and streams are named under
    the installation from here on. Only the control plane is given a pass that runs:
    every other process's pass is a wake."""
    async with pool.connection() as connection:
        installation = await installation_of(connection)
    settings = await settings_of(pool)
    missing = [name for name in ("workspaces", "mirrors", "image") if not settings.get(name)]
    if missing:
        raise NotConfigured(f"the installation has not been told its {missing}: run `relay init`")
    name_attempts_under(installation)
    mirrors = Path(settings["mirrors"])
    workspaces = Workspaces(Path(settings["workspaces"]))
    definitions = [Path(each) for each in settings.get("definitions", [])]
    oracle = ProcessOracle(ContainerIsolation(settings["image"]))
    behaviours = Behaviours(pool, mirrors, oracle)
    reviews = Reviews(pool, mirrors)
    integration = Integration(pool, mirrors, oracle)
    invocations = Invocations(pool)
    features = (Features if control_plane else Elsewhere)(
        pool, reviews, integration, invocations=invocations, behaviours=behaviours
    )
    if isinstance(features, Elsewhere):
        features.installation = installation
    decisions = Decisions(pool, features)

    def authoring(role: str) -> tuple[Any, ...]:
        return (pool, invocations, workspaces, mirrors, definitions, profile_of(settings, role))

    roadmap = RoadmapTask(*authoring("analyst"))
    build = BuildTask(
        pool,
        behaviours,
        invocations,
        workspaces,
        mirrors,
        definitions,
        profile_of(settings, "builder"),
    )
    tasks: dict[str, dict[str, Any]] = {
        "analyst": {
            "plan": roadmap,
            "revise": roadmap,
            "correct": roadmap,
            "assess": AssessTask(*authoring("analyst")),
            "reconfirm": ReconfirmTask("analyst", decisions, *authoring("analyst")),
        },
        "verifier": {
            "specify": SpecifyTask(*authoring("verifier")),
            "seal": SealTask(*authoring("verifier")),
            "reconfirm": ReconfirmTask("verifier", decisions, *authoring("verifier")),
        },
        "builder": {"build": build, "remediate": build},
    }
    for reviewer in ("qa", "sentinel"):
        tasks[reviewer] = {
            "review": ReviewTask(
                pool, reviews, invocations, workspaces, definitions, profile_of(settings, reviewer)
            )
        }
    every: dict[Any, Any] = {}
    for role, of_role in tasks.items():
        for kind, task in of_role.items():
            every[(kind, role) if kind in ("reconfirm", "review") else kind] = task
    Tasks(every).attach(invocations)
    return Relay(
        pool=pool,
        installation=installation,
        settings=settings,
        streams=Streams(broker, prefix=installation),
        claims=Assignments(pool),
        stories=Stories(pool),
        behaviours=behaviours,
        reviews=reviews,
        integration=integration,
        invocations=invocations,
        features=features,
        decisions=decisions,
        boundaries=Boundaries(pool),
        tasks=tasks,
    )
