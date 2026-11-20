"""A yes-or-no question to Git has three outcomes: yes, no, and no answer. Only the first
two are answers."""

from pathlib import Path

import pytest

from relay.git import checkout
from relay.git.checkout import CheckoutFailed, NotStarted, git_answers_yes, git_result


async def test_exit_zero_is_yes_and_exit_one_is_no(tmp_path: Path) -> None:
    assert await git_answers_yes("-C", str(tmp_path), "init", "--quiet") is True
    assert await git_answers_yes("-C", str(tmp_path), "cat-file", "-e", "0" * 39 + "1") is False


async def test_any_other_exit_is_git_failing_to_answer(tmp_path: Path) -> None:
    with pytest.raises(CheckoutFailed, match="cat-file"):
        await git_answers_yes("-C", str(tmp_path / "not-a-repository"), "cat-file", "-e", "HEAD")


async def test_a_git_that_could_not_be_started_is_told_apart_from_one_that_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_file_descriptors(*_: object, **__: object) -> None:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(checkout.asyncio, "create_subprocess_exec", no_file_descriptors)

    with pytest.raises(NotStarted, match="could not be started"):
        await git_result("push", "somewhere")
