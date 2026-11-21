"""One Relay project per managed trunk (decision D-9). What a candidate must still
satisfy is recorded per project, and a project's lock protects only its own pushes, so a
branch that two projects wrote to would be checked by neither."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application.behaviours import Behaviours
from relay.git.checkout import canonical_repository
from relay.testing.oracle import ContainerIsolation, ProcessOracle


@pytest.fixture
def behaviours(database: AsyncConnectionPool, tmp_path: Path, runner_image: str) -> Behaviours:
    return Behaviours(
        database, tmp_path / "mirrors", ProcessOracle(ContainerIsolation(runner_image))
    )


def bare(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    return path


async def test_a_trunk_is_managed_by_one_project(behaviours: Behaviours, tmp_path: Path) -> None:
    repository = bare(tmp_path / "vat.git")
    await behaviours.open_project("checkout", str(repository))

    with pytest.raises(ValueError, match="already managed"):
        await behaviours.open_project("billing", str(repository))

    await behaviours.open_project("billing", str(repository), trunk="release")


async def test_a_path_and_a_file_url_to_it_are_the_same_repository(
    behaviours: Behaviours, tmp_path: Path
) -> None:
    repository = bare(tmp_path / "vat.git")
    assert canonical_repository(repository.as_uri()) == canonical_repository(str(repository))
    await behaviours.open_project("checkout", str(repository))

    with pytest.raises(ValueError, match="already managed"):
        await behaviours.open_project("billing", repository.as_uri())

    await behaviours.open_project("billing", repository.as_uri(), trunk="release")
