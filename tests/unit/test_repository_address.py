"""A project's repository is known by one address, whatever directory Relay runs in."""

from pathlib import Path

import pytest

from relay.git.checkout import canonical_repository


def test_a_relative_local_path_is_made_absolute_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert canonical_repository("remote.git") == str(tmp_path.resolve() / "remote.git")
    assert canonical_repository("./nested/remote.git") == str(
        tmp_path.resolve() / "nested" / "remote.git"
    )


@pytest.mark.parametrize(
    "address",
    [
        "git@github.com:someone/project.git",
        "https://example.com/someone/project.git",
        "ssh://git@example.com/someone/project.git",
        "file://another-host/srv/git/project.git",
    ],
)
def test_a_remote_address_is_kept_as_given(address: str) -> None:
    assert canonical_repository(address) == address


@pytest.mark.parametrize(
    "url", ["file:///srv/git/my%20project.git", "file://localhost/srv/git/my%20project.git"]
)
def test_a_file_url_to_this_machine_is_the_repository_at_that_path(url: str) -> None:
    assert canonical_repository(url) == canonical_repository("/srv/git/my project.git")


def test_an_address_carrying_credentials_is_still_refused() -> None:
    with pytest.raises(ValueError, match="credentials"):
        canonical_repository("https://user:secret@example.com/project.git")
