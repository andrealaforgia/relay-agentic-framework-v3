"""Real PostgreSQL and Redis from compose.yaml. Each test gets its own schema and key prefix."""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import redis.asyncio as redis
from psycopg_pool import AsyncConnectionPool

from relay.persistence.database import migrate, open_pool

DATABASE_URL = os.environ.get(
    "RELAY_TEST_DATABASE_URL", "postgresql://relay:relay-dev-only@localhost:55432/relay"
)
REDIS_URL = os.environ.get("RELAY_TEST_REDIS_URL", "redis://localhost:56379/0")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in str(item.path):
            item.add_marker(pytest.mark.integration)


@pytest.fixture
def namespace() -> str:
    return f"t{uuid.uuid4().hex[:12]}"


@pytest.fixture
async def database(namespace: str) -> AsyncIterator[AsyncConnectionPool]:
    async with open_pool(DATABASE_URL) as admin, admin.connection() as connection:
        await connection.execute(f'CREATE SCHEMA "{namespace}"')
    async with open_pool(DATABASE_URL, schema=namespace) as pool:
        await migrate(pool)
        yield pool
    async with open_pool(DATABASE_URL) as admin, admin.connection() as connection:
        # A test that fails halfway through a choreography of held transactions leaves
        # a session holding locks, and dropping the schema would wait for it for ever.
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE application_name = %s AND pid <> pg_backend_pid()",
            (f"relay:{namespace}",),
        )
        await connection.execute(f'DROP SCHEMA "{namespace}" CASCADE')


@pytest.fixture
async def broker(namespace: str) -> AsyncIterator[redis.Redis]:
    client = redis.from_url(REDIS_URL, decode_responses=True)
    yield client
    keys = [key async for key in client.scan_iter(f"{namespace}:*")]
    if keys:
        await client.delete(*keys)
    await client.aclose()


@pytest.fixture(scope="session")
def runner_image() -> str:
    """A digest-pinned image for the oracle's container."""
    subprocess.run(["docker", "pull", "-q", "python:3.14-alpine"], check=True, capture_output=True)
    return subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "python:3.14-alpine",
            "--format",
            "{{index .RepoDigests 0}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
async def delivering_world(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> AsyncIterator[Any]:
    """A project whose every role is a worker's: see `test_workers_deliver`."""
    from tests.integration.completion_world import CompletionWorld
    from tests.integration.test_workers_deliver import Delivering

    world = CompletionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    delivering = Delivering(world, tmp_path)
    yield delivering
    await delivering.invocations.close()
    for session in delivering.sessions:
        await session.stop()


@pytest.fixture
async def building(
    database: AsyncConnectionPool, tmp_path: Path, runner_image: str
) -> AsyncIterator[Any]:
    """A behaviour with a verified red, waiting for its Builder: see `test_build_task`."""
    from tests.integration.decision_world import DecisionWorld
    from tests.integration.story_world import STANDARD_RATE
    from tests.integration.test_build_task import Building

    world = DecisionWorld(database, tmp_path, runner_image)
    world.base = await world.open()
    await world.decide(await world.roadmap(STANDARD_RATE))
    await world.awaiting_builder("B1")
    building = Building(world, tmp_path)
    yield building
    await building.invocations.close()
    for session in building.sessions:
        await session.stop()
