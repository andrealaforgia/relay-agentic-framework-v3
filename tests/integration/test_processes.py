"""Relay as separate operating-system processes: a control plane and a worker for each
role, with nothing in common but the database, Redis and the filesystem. A feature is
delivered between them, and part-way through the control plane loses its hold on the
installation and is replaced while it is still alive. The provider is a stand-in that
acts each role; everything else is real."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import redis.asyncio as redis
from psycopg_pool import AsyncConnectionPool

from relay.application.boundaries import Boundaries
from relay.application.stories import Stories
from relay.domain.story import HumanAction
from relay.providers.boundary_suite import Observation, SuiteResult, runtime_now
from relay.providers.confinement import boundary_digest, confinement_for
from relay.runtime import ROLES, configure, wake
from tests.integration.completion_world import CompletionWorld
from tests.integration.conftest import DATABASE_URL, REDIS_URL
from tests.integration.fixtures import PRODUCT_OWNER, VAT_APPLIES_STANDARD_RATE
from tests.integration.test_roadmap_task import S1, proposing
from tests.integration.test_workers_deliver import AGENTS, B1, DEMONSTRATED, VOUCHED
from tests.integration.trunk_world import PROJECT

ACTING = Path(__file__).resolve().parent.parent / "fixtures" / "claude" / "acting_claude.py"
STATED = "none: stated by the test of the processes"
NOTHING_FOUND = {
    "outcome": "delivered",
    "assumptions": [],
    "delivered": {"findings": [], "carried": []},
}
SCRIPT = {
    "You propose the roadmap": [proposing(S1)],
    "You are asked to assess": [VOUCHED],
    "You decide how a user story is shown": [B1, DEMONSTRATED],
    "You are asked to vouch": [VOUCHED],
    "You are the Builder": [
        {
            "__files__": {"vat.py": VAT_APPLIES_STANDARD_RATE},
            "reply": {
                "outcome": "delivered",
                "assumptions": [],
                "delivered": {"note": "applied the standard rate"},
            },
        }
    ],
    "for the tests and the design of the tests": [NOTHING_FOUND],
    "for security": [NOTHING_FOUND],
}


class Running:
    """The processes of one installation, as an operator would have started them."""

    def __init__(self, world: CompletionWorld, namespace: str, tmp_path: Path) -> None:
        self.world, self.tmp = world, tmp_path
        self.environment = {
            **os.environ,
            "RELAY_DATABASE_URL": DATABASE_URL,
            "RELAY_REDIS_URL": REDIS_URL,
            "RELAY_DATABASE_SCHEMA": namespace,
        }
        self.started: list[subprocess.Popen[bytes]] = []
        self.stories = Stories(world.database)
        self.requests = 0
        self.installation = ""

    def start(self, *command: str) -> subprocess.Popen[bytes]:
        log = open(self.tmp / f"{'-'.join(command)}-{len(self.started)}.log", "wb")  # noqa: SIM115
        process = subprocess.Popen(
            [sys.executable, "-m", "relay.cli.main", *command],
            env=self.environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self.started.append(process)
        return process

    def said(self, process: subprocess.Popen[bytes]) -> str:
        at = self.started.index(process)
        [log] = list(self.tmp.glob(f"*-{at}.log"))
        return log.read_text()

    async def until(self, what: str, sql: str, wanted: object, within: float = 240.0) -> None:
        async with asyncio.timeout(within):
            while await self.world.column(sql) != [wanted]:
                for process in self.started[1:6]:
                    assert process.poll() is None, self.said(process)
                await asyncio.sleep(0.3)

    async def decide(self, gate: Any, action: str = "approve") -> None:
        """A person's decision, made by a command: it commits, wakes the control plane,
        and is gone."""
        self.requests += 1
        await self.stories.decide(
            HumanAction(PRODUCT_OWNER, f"process-test-{self.requests}", str(gate), action, {})
        )
        await wake(self.world.database, self.installation, PROJECT)

    def stop_all(self) -> None:
        for process in self.started:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        for process in self.started:
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()


@pytest.fixture
async def running(
    database: AsyncConnectionPool,
    broker: redis.Redis,
    namespace: str,
    tmp_path: Path,
    runner_image: str,
) -> AsyncIterator[Running]:
    world = CompletionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    cli = tmp_path / "cli" / "claude"
    cli.parent.mkdir()
    shutil.copy(ACTING, cli)
    (cli.parent / "script.json").write_text(json.dumps(SCRIPT))
    agents = tmp_path / "definitions"
    agents.mkdir()
    for name in AGENTS:
        (agents / f"{name}.md").write_text(f"---\nname: {name}\n---\nDo the work.\n")
    workspaces = tmp_path.resolve() / "workspaces"
    await configure(
        database,
        workspaces=str(workspaces),
        mirrors=str(world.mirrors),
        image=runner_image,
        definitions=[str(agents)],
        cli=str(cli),
        profiles={"default": {"model": "claude-haiku-5-5", "timeout": 120}},
    )
    async with database.connection() as connection:
        await connection.execute(
            "UPDATE installation SET policy = jsonb_build_object('statement', %s::text)", (STATED,)
        )
        row = await (
            await connection.execute("SELECT installation_id::text AS id FROM installation")
        ).fetchone()
    assert row is not None
    # As `relay doctor --boundary` will: the bounds of each role, seen to hold on this
    # runtime. Here they are taken as seen; the suite itself is 3b's.
    runtime = runtime_now(STATED, str(cli))
    for role in ROLES:
        withheld = [world.mirrors] if role == "builder" else []
        bounds = confinement_for(role, workspaces / "run" / "tree", withheld=withheld)
        await Boundaries(database).record(
            SuiteResult(
                role,
                boundary_digest(bounds, runtime),
                runtime,
                (Observation("taken as seen by the test of the processes", True, ""),),
                None,
            )
        )
    running = Running(world, namespace, tmp_path)
    running.installation = row["id"]
    yield running
    running.stop_all()
    keys = [key async for key in broker.scan_iter(f"{row['id']}:*")]
    if keys:
        await broker.delete(*keys)


@pytest.mark.timeout(900)
async def test_a_feature_is_delivered_between_processes_and_through_a_change_of_control_plane(
    running: Running,
) -> None:
    """3C-1.1, 3C-1.2: the Builder's worker has no oracle and publishes nothing. The
    control plane does both. And a control plane whose lock connection is cut, alive, is
    replaced: the second one finishes the feature, and the first stops by itself."""
    world = running.world
    first = running.start("up", "--every", "2")
    for role in ROLES:
        running.start("worker", "start", "--role", role)
    await world.stories.open_feature(PRODUCT_OWNER, PROJECT, "F1", "prices need VAT")
    await wake(world.database, running.installation, PROJECT)

    await running.until(
        "the roadmap is put to the person", "SELECT count(*) FROM gates WHERE state = 'open'", 1
    )
    [roadmap] = await world.stories.inbox(PROJECT)
    assert roadmap.kind == "roadmap"
    await running.decide(roadmap.gate_id)
    # The first control plane loses its hold: the connection its lock is on is ended
    # under it, as a network fault would. The process is alive and its pool works.
    await running.until("a term is open", "SELECT term FROM installation", 1)
    await world.column(
        "SELECT pg_terminate_backend(l.pid) FROM pg_locks l WHERE l.locktype = 'advisory' "
        "AND l.objsubid = 2 AND l.granted AND l.pid <> pg_backend_pid()"
    )
    second = running.start("up", "--every", "2")

    await running.until("a new term is open", "SELECT term FROM installation", 2)
    await running.until(
        "the story is put to the person",
        "SELECT count(*) FROM gates WHERE state = 'open' AND kind = 'story'",
        1,
    )
    [story] = await world.stories.inbox(PROJECT)
    await running.decide(story.gate_id)
    await running.until(
        "the feature is completed",
        "SELECT state FROM features WHERE feature_id = 'F1'",
        "completed",
    )

    on_trunk = world.git("show", f"{world.trunk()}:vat.py", at=world.remote)
    assert on_trunk == VAT_APPLIES_STANDARD_RATE.strip()
    # The first control plane stopped by itself, saying why; the second is still serving.
    assert first.wait(timeout=30) == 0, running.said(first)
    assert "This control plane stops" in running.said(first)
    assert second.poll() is None
    # One attempt was published and one green took effect, for one call of the Builder.
    assert await world.column("SELECT count(*) FROM invocations WHERE role = 'builder'") == [1]
    assert await world.column(
        "SELECT count(*) FROM events WHERE event_type = 'behaviour.green_verified'"
    ) == [1]
