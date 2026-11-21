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

    with pytest.raises(ValueError, match="no Git repository"):
        canonical_repository(str(work_tree / "docs"))


async def test_a_repository_named_without_its_dot_git_is_still_that_repository(
    behaviours: Behaviours, tmp_path: Path
) -> None:
    """3AII-S4-2.1."""
    repository = bare(tmp_path / "vat.git")
    assert git_reads(str(tmp_path / "vat"))
    await behaviours.open_project("checkout", str(repository))

    with pytest.raises(ValueError, match="already managed"):
        await behaviours.open_project("billing", str(tmp_path / "vat"))


async def test_a_project_is_not_opened_on_a_path_with_no_repository(
    behaviours: Behaviours, tmp_path: Path
) -> None:
    """3AII-S4-2.1: enrolled now, it would be known by another name once it existed."""
    with pytest.raises(ValueError, match="no Git repository"):
        await behaviours.open_project("checkout", str(tmp_path / "not-yet"))


@pytest.mark.parametrize("address", ["", "file://"])
async def test_an_address_that_names_no_path_opens_no_project(
    behaviours: Behaviours,
    database: AsyncConnectionPool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    """3AII-S4-3.1. Run from inside a repository, which must not be enrolled unnamed."""
    monkeypatch.chdir(bare(tmp_path / "here.git"))

    with pytest.raises(ValueError, match="names no path"):
        await behaviours.open_project("checkout", address)

    async with database.connection() as connection:
        assert await (await connection.execute("SELECT 1 FROM projects")).fetchone() is None
        assert await (await connection.execute("SELECT 1 FROM events")).fetchone() is None
