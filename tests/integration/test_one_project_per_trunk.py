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


def with_a_work_tree(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def git_reads(address: str) -> bool:
    listed = subprocess.run(["git", "ls-remote", address], capture_output=True, check=False)
    return listed.returncode == 0


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


async def test_a_file_url_naming_a_host_is_still_the_repository_on_this_machine(
    behaviours: Behaviours, tmp_path: Path
) -> None:
    """3AII-S4-1.1."""
    repository = bare(tmp_path / "vat.git")
    loopback = f"file://127.0.0.1{repository.resolve()}"
    assert git_reads(loopback)
    await behaviours.open_project("checkout", str(repository))

    with pytest.raises(ValueError, match="already managed"):
        await behaviours.open_project("billing", loopback)


async def test_a_work_tree_and_the_git_directory_inside_it_are_the_same_repository(
    behaviours: Behaviours, tmp_path: Path
) -> None:
    """3AII-S4-1.1."""
    work_tree = with_a_work_tree(tmp_path / "vat")
    assert canonical_repository(str(work_tree)) == canonical_repository(str(work_tree / ".git"))
    await behaviours.open_project("checkout", str(work_tree))

    with pytest.raises(ValueError, match="already managed"):
        await behaviours.open_project("billing", str(work_tree / ".git"))


def test_a_directory_inside_a_repository_is_not_that_repository(tmp_path: Path) -> None:
    work_tree = with_a_work_tree(tmp_path / "vat")
    (work_tree / "docs").mkdir()

    assert canonical_repository(str(work_tree / "docs")) == str((work_tree / "docs").resolve())
