"""A yes-or-no question to Git has three outcomes: yes, no, and no answer. Only the first
two are answers."""

from pathlib import Path

import pytest

from relay.git.checkout import CheckoutFailed, git_answers_yes


async def test_exit_zero_is_yes_and_exit_one_is_no(tmp_path: Path) -> None:
    assert await git_answers_yes("-C", str(tmp_path), "init", "--quiet") is True
    assert await git_answers_yes("-C", str(tmp_path), "cat-file", "-e", "0" * 39 + "1") is False


async def test_any_other_exit_is_git_failing_to_answer(tmp_path: Path) -> None:
    with pytest.raises(CheckoutFailed, match="cat-file"):
        await git_answers_yes("-C", str(tmp_path / "not-a-repository"), "cat-file", "-e", "HEAD")
