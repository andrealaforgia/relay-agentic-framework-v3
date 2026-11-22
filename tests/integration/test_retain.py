"""Keeping a builder's workspace as a commit. The supervisor makes the commit: the
builder is given no Git metadata and is trusted with none."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from relay.git.checkout import CheckoutFailed
from relay.git.mirror import Mirror
from tests.integration.fixtures import commit


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
async def based(tmp_path: Path) -> tuple[Mirror, str, Path]:
    """A mirror holding one commit, and a workspace exported from it."""
    origin = tmp_path / "origin"
    base = commit(origin, {"vat.py": "print(100)\n", "old.txt": "to be removed\n"})
    mirror = Mirror(tmp_path / "mirror.git")
    await mirror.pin_from(str(origin), "refs/heads/main", base)
    workspace = await mirror.export(base, tmp_path / "ws" / "tree")
    return mirror, base, workspace


async def test_what_is_in_the_workspace_becomes_a_commit_on_its_base(
    based: tuple[Mirror, str, Path], tmp_path: Path
) -> None:
    mirror, base, workspace = based
    (workspace / "vat.py").write_text("print(120)\n")
    (workspace / "new.txt").write_text("added\n")
    (workspace / "old.txt").unlink()

    kept = await mirror.retain(workspace, base, "refs/relay/invocations/one")

    held = tmp_path / "mirror.git"
    assert git(held, "rev-parse", "refs/relay/invocations/one") == kept
    assert git(held, "rev-parse", f"{kept}^") == base
    assert git(held, "ls-tree", "--name-only", kept).split() == ["new.txt", "vat.py"]
    assert git(held, "show", f"{kept}:vat.py") == "print(120)"
    assert await mirror.holds(kept)
    exported = await mirror.export(kept, tmp_path / "again")
    assert (exported / "new.txt").read_text() == "added\n"


async def test_a_workspace_nothing_was_changed_in_is_still_a_commit_of_its_own(
    based: tuple[Mirror, str, Path], tmp_path: Path
) -> None:
    mirror, base, workspace = based

    kept = await mirror.retain(workspace, base, "refs/relay/invocations/one")

    assert kept != base
    assert git(tmp_path / "mirror.git", "rev-parse", f"{kept}^{{tree}}") == git(
        tmp_path / "mirror.git", "rev-parse", f"{base}^{{tree}}"
    )


async def test_a_link_is_kept_as_a_link_and_not_followed(
    based: tuple[Mirror, str, Path], tmp_path: Path
) -> None:
    mirror, base, workspace = based
    secret = tmp_path / "secret.txt"
    secret.write_text("the supervisor's\n")
    (workspace / "leak").symlink_to(secret)

    kept = await mirror.retain(workspace, base, "refs/relay/invocations/one")

    held = tmp_path / "mirror.git"
    assert git(held, "ls-tree", kept, "leak").startswith("120000 blob")
    assert git(held, "cat-file", "-p", f"{kept}:leak") == str(secret)  # the path, not the content


@pytest.mark.parametrize("where", [".git", "sub/.git"])
async def test_a_workspace_that_has_grown_git_metadata_is_refused(
    based: tuple[Mirror, str, Path], where: str
) -> None:
    mirror, base, workspace = based
    (workspace / where).mkdir(parents=True)
    (workspace / where / "config").write_text("[core]\n\thooksPath = /tmp/evil\n")

    with pytest.raises(CheckoutFailed, match="Git metadata"):
        await mirror.retain(workspace, base, "refs/relay/invocations/one")


async def test_a_base_the_mirror_does_not_hold_is_refused(based: tuple[Mirror, str, Path]) -> None:
    mirror, _, workspace = based

    with pytest.raises(CheckoutFailed, match="not held"):
        await mirror.retain(workspace, "a" * 40, "refs/relay/invocations/one")


async def test_what_is_executable_in_the_workspace_is_executable_in_the_commit(
    based: tuple[Mirror, str, Path], tmp_path: Path
) -> None:
    mirror, base, workspace = based
    (workspace / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (workspace / "run.sh").chmod(0o755)

    kept = await mirror.retain(workspace, base, "refs/relay/invocations/one")

    assert git(tmp_path / "mirror.git", "ls-tree", kept, "run.sh").startswith("100755")
