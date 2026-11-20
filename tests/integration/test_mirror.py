"""A commit Relay has judged stays available to Relay, whatever happens to the ref that
first made it reachable."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from relay.git.checkout import CheckoutFailed
from relay.git.mirror import Mirror
from tests.integration.fixtures import (
    VAT_APPLIES_STANDARD_RATE,
    VAT_RETURNS_NET,
    commit,
    publish_attempt,
    publish_tag_as_attempt,
)

ATTEMPT = "refs/relay/attempts/vat-calculator/B1/1"


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


def expire_everything_unreachable(repository: Path) -> None:
    git(repository, "reflog", "expire", "--expire=now", "--all")
    git(repository, "gc", "--quiet", "--prune=now")


def subject_with_candidate(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "subject"
    trunk = commit(repository, {"vat.py": VAT_RETURNS_NET})
    candidate = publish_attempt(
        repository, "B1", 1, {"vat.py": VAT_APPLIES_STANDARD_RATE}, on=trunk
    )
    return repository, trunk, candidate


async def test_a_pinned_commit_survives_the_builder_moving_the_ref_and_git_pruning(
    tmp_path: Path,
) -> None:
    repository, trunk, candidate = subject_with_candidate(tmp_path)
    mirror = Mirror(tmp_path / "control-plane" / "vat.git")
    await mirror.pin_from(str(repository), ATTEMPT, candidate)

    publish_attempt(repository, "B1", 1, {"vat.py": "print('something else')\n"}, on=trunk)
    restarted = Mirror(tmp_path / "control-plane" / "vat.git")
    expire_everything_unreachable(tmp_path / "control-plane" / "vat.git")

    assert await restarted.holds(candidate)
    exported = await restarted.export(candidate, tmp_path / "exported")
    assert (exported / "vat.py").read_text() == VAT_APPLIES_STANDARD_RATE
    assert sorted(entry.name for entry in exported.iterdir()) == ["vat.py"]


async def test_publishing_creates_exactly_one_ref_and_leaves_the_rest_alone(tmp_path: Path) -> None:
    repository, trunk, candidate = subject_with_candidate(tmp_path)
    mirror = Mirror(tmp_path / "control-plane" / "vat.git")
    await mirror.pin_from(str(repository), ATTEMPT, candidate)
    before = git(repository, "for-each-ref")

    await mirror.publish(str(repository), candidate, "refs/relay/candidates/vat-calculator/B1/1")

    after = git(repository, "for-each-ref").splitlines()
    assert sorted(set(after) - set(before.splitlines())) == [
        f"{candidate} commit\trefs/relay/candidates/vat-calculator/B1/1"
    ]
    assert set(before.splitlines()) <= set(after)
    assert git(repository, "rev-parse", "refs/heads/main") == trunk


async def test_publishing_refuses_to_replace_an_existing_ref(tmp_path: Path) -> None:
    repository, trunk, candidate = subject_with_candidate(tmp_path)
    mirror = Mirror(tmp_path / "control-plane" / "vat.git")
    await mirror.pin_from(str(repository), ATTEMPT, candidate)
    git(repository, "update-ref", "refs/relay/candidates/vat-calculator/B1/1", trunk)

    with pytest.raises(CheckoutFailed):
        await mirror.publish(
            str(repository), candidate, "refs/relay/candidates/vat-calculator/B1/1"
        )

    assert git(repository, "rev-parse", "refs/relay/candidates/vat-calculator/B1/1") == trunk


async def test_a_ref_that_is_not_at_the_expected_commit_is_not_pinned(tmp_path: Path) -> None:
    repository, trunk, candidate = subject_with_candidate(tmp_path)
    mirror = Mirror(tmp_path / "control-plane" / "vat.git")

    with pytest.raises(CheckoutFailed, match="moved"):
        await mirror.pin_from(str(repository), ATTEMPT, trunk)

    assert not await mirror.holds(candidate)
    assert git(tmp_path / "control-plane" / "vat.git", "for-each-ref") == ""


async def test_a_tag_is_not_pinned_as_a_candidate(tmp_path: Path) -> None:
    repository, trunk, _ = subject_with_candidate(tmp_path)
    tag_object = publish_tag_as_attempt(repository, "B1", 2, pointing_at=trunk)
    mirror = Mirror(tmp_path / "control-plane" / "vat.git")

    with pytest.raises(CheckoutFailed, match="not a commit"):
        await mirror.pin_from(
            str(repository), "refs/relay/attempts/vat-calculator/B1/2", tag_object
        )

    assert git(tmp_path / "control-plane" / "vat.git", "for-each-ref") == ""


async def test_a_replacement_object_cannot_change_what_a_pinned_hash_exports(
    tmp_path: Path,
) -> None:
    repository, trunk, candidate = subject_with_candidate(tmp_path)
    mirror_path = tmp_path / "control-plane" / "vat.git"
    mirror = Mirror(mirror_path)
    await mirror.pin_from(str(repository), ATTEMPT, candidate)
    await mirror.pin_from(str(repository), "refs/heads/main", trunk)
    git(mirror_path, "replace", candidate, trunk)

    exported = await mirror.export(candidate, tmp_path / "exported")

    assert (exported / "vat.py").read_text() == VAT_APPLIES_STANDARD_RATE


async def test_ancestry_is_answered_from_the_mirror(tmp_path: Path) -> None:
    repository, trunk, candidate = subject_with_candidate(tmp_path)
    mirror = Mirror(tmp_path / "control-plane" / "vat.git")
    await mirror.pin_from(str(repository), ATTEMPT, candidate)

    assert await mirror.descends(candidate, trunk)
    assert not await mirror.descends(trunk, candidate)
