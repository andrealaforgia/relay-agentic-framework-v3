"""A project's repository is known by one address, whatever directory Relay runs in and
however the address was written."""

import subprocess
from pathlib import Path

import pytest

from relay.git.checkout import canonical_repository


def bare(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    return path.resolve()


def test_a_relative_local_path_is_made_absolute_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = bare(tmp_path / "remote.git")
    nested = bare(tmp_path / "nested" / "remote.git")
    monkeypatch.chdir(tmp_path)

    assert canonical_repository("remote.git") == str(here)
    assert canonical_repository("./nested/remote.git") == str(nested)


@pytest.mark.parametrize(
    "address",
    [
        "git@github.com:someone/project.git",
        "https://example.com/someone/project.git",
        "ssh://git@example.com/someone/project.git",
    ],
)
def test_a_remote_address_is_kept_as_given(address: str) -> None:
    assert canonical_repository(address) == address


@pytest.mark.parametrize("host", ["", "localhost", "127.0.0.1", "another-host"])
def test_a_file_url_is_the_repository_at_that_path_whatever_host_it_names(
    tmp_path: Path, host: str
) -> None:
    """Git reads a file URL from this machine whatever host it names."""
    repository = bare(tmp_path / "my project.git")
    url = f"file://{host}{str(repository).replace(' ', '%20')}"

    assert canonical_repository(url) == str(repository)


def test_a_name_git_completes_with_dot_git_is_the_same_repository(tmp_path: Path) -> None:
    """3AII-S4-2.1."""
    repository = bare(tmp_path / "remote.git")

    assert canonical_repository(str(tmp_path / "remote")) == str(repository)


def test_a_name_ending_in_a_space_is_not_the_repository_beside_it(tmp_path: Path) -> None:
    """3AII-S4-2.2."""
    plain = bare(tmp_path / "vat.git")
    spaced = bare(tmp_path / "vat.git ")

    assert canonical_repository(str(spaced)) == str(spaced) != str(plain)


def test_a_path_with_no_repository_at_it_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no Git repository"):
        canonical_repository(str(tmp_path / "not-yet"))


def test_an_address_carrying_credentials_is_still_refused() -> None:
    with pytest.raises(ValueError, match="credentials"):
        canonical_repository("https://user:secret@example.com/project.git")


@pytest.mark.parametrize("address", ["", "file://", "file://localhost"])
def test_an_address_that_names_no_path_is_not_the_directory_relay_runs_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    """3AII-S4-3.1."""
    monkeypatch.chdir(bare(tmp_path / "here.git"))

    with pytest.raises(ValueError, match="names no path"):
        canonical_repository(address)


def test_a_repository_that_cannot_be_read_is_not_swapped_for_the_one_beside_it(
    tmp_path: Path,
) -> None:
    """3AII-S4-3.2."""
    damaged = bare(tmp_path / "vat")
    bare(tmp_path / "vat.git")
    (damaged / "config").write_text("[core\n")

    with pytest.raises(ValueError, match="can be read"):
        canonical_repository(str(damaged))
