"""The canonical agent definitions are used as they are installed, and every invocation
records exactly which one it used: where it was, what it said, and which revision of its
repository that was."""

import hashlib
import subprocess
from pathlib import Path

import pytest

from relay.providers.definitions import DEFAULT_DEFINITIONS, find_definition, search_path

REVIEWER = """---
name: alf-test-design-reviewer
description: Evaluates test code quality.
tools: Read, Bash, Glob, Grep
skills:
  - farley-properties
---

# test-design-reviewer

You review tests.
"""


def installed(
    directory: Path, name: str = "alf-test-design-reviewer", text: str = REVIEWER
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(text)
    return path


def git(directory: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), "-c", "user.name=t", "-c", "user.email=t@relay.test", *arguments],
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


def test_a_definition_is_read_with_its_path_its_hash_and_what_it_tells_the_agent(
    tmp_path: Path,
) -> None:
    path = installed(tmp_path / "agents")

    found = find_definition("alf-test-design-reviewer", [tmp_path / "agents"])

    assert found.name == "alf-test-design-reviewer"
    assert found.path == path.resolve()
    assert found.sha256 == hashlib.sha256(REVIEWER.encode()).hexdigest()
    assert found.description == "Evaluates test code quality."
    assert found.prompt == "# test-design-reviewer\n\nYou review tests.\n"
    assert (found.revision, found.dirty) == (None, False)  # not in a repository


def test_the_first_place_that_has_it_wins(tmp_path: Path) -> None:
    installed(tmp_path / "global")
    configured = installed(tmp_path / "configured", text=REVIEWER + "\nStricter here.\n")

    found = find_definition(
        "alf-test-design-reviewer", [tmp_path / "configured", tmp_path / "global"]
    )

    assert found.path == configured.resolve()
    assert found.prompt.endswith("Stricter here.\n")


def test_a_role_with_no_definition_installed_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LookupError, match="alf-security-assessor"):
        find_definition("alf-security-assessor", [tmp_path / "agents"])


def test_a_name_that_could_leave_the_directory_is_refused(tmp_path: Path) -> None:
    installed(tmp_path, "secrets")

    with pytest.raises(ValueError, match="agent name"):
        find_definition("../secrets", [tmp_path / "agents"])


def test_the_revision_of_its_repository_is_recorded_and_whether_the_file_differs_from_it(
    tmp_path: Path,
) -> None:
    path = installed(tmp_path / "repo" / "agents")
    git(tmp_path / "repo", "init", "-q")
    git(tmp_path / "repo", "add", "-A")
    git(tmp_path / "repo", "commit", "-q", "-m", "definitions")
    head = git(tmp_path / "repo", "rev-parse", "HEAD")

    committed = find_definition("alf-test-design-reviewer", [tmp_path / "repo" / "agents"])
    path.write_text(REVIEWER + "\nEdited locally.\n")
    edited = find_definition("alf-test-design-reviewer", [tmp_path / "repo" / "agents"])

    assert (committed.revision, committed.dirty) == (head, False)
    assert (edited.revision, edited.dirty) == (head, True)
    assert edited.sha256 != committed.sha256


def test_a_file_that_is_in_a_repository_and_not_committed_to_it_is_dirty(tmp_path: Path) -> None:
    git_root = tmp_path / "repo"
    git_root.mkdir()
    git(git_root, "init", "-q")
    git(git_root, "commit", "-q", "--allow-empty", "-m", "empty")
    installed(git_root / "agents")

    found = find_definition("alf-test-design-reviewer", [git_root / "agents"])

    assert found.revision is not None and found.dirty


def test_a_file_with_no_front_matter_is_all_prompt(tmp_path: Path) -> None:
    installed(tmp_path, "plain", "Just do the work.\n")

    found = find_definition("plain", [tmp_path])

    # Under its name: the real CLI refuses an agent with an empty description, and only
    # after it has been launched (found by the first smoke against the real provider).
    assert (found.description, found.prompt) == ("The plain agent.", "Just do the work.\n")


def test_the_search_goes_from_what_was_configured_to_the_project_to_the_home(
    tmp_path: Path,
) -> None:
    assert search_path(
        [tmp_path / "mine"], project=tmp_path / "project", home=tmp_path / "home"
    ) == [
        tmp_path / "mine",
        tmp_path / "project" / ".claude" / "agents",
        tmp_path / "home" / ".claude" / "agents",
    ]


def test_the_roles_default_to_the_definitions_the_specification_names() -> None:
    assert DEFAULT_DEFINITIONS == {
        "analyst": "alf-user-story-writer",
        "verifier": "alf-atdd-developer",
        "builder": "alf-atdd-developer",
        "qa": "alf-test-design-reviewer",
        "sentinel": "alf-security-assessor",
    }
