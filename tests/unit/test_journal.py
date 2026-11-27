"""The journal of requests: handles to what a person asked for, kept on their machine."""

from __future__ import annotations

from pathlib import Path

from relay.cli import journal


def test_a_request_is_written_down_with_what_it_was_resolved_to_and_found_again(
    tmp_path: Path,
) -> None:
    file = tmp_path / "state" / "requests.jsonl"

    first = journal.note(file, ["approve", "G1"], "installation-a", "vat")
    second = journal.note(file, ["policy", "state", "none"], "installation-a", None)

    assert journal.read(file) == [first, second]
    assert first.request_id != second.request_id
    assert (first.command, first.installation, first.project) == (
        ("approve", "G1"),
        "installation-a",
        "vat",
    )
    assert oct(file.stat().st_mode & 0o777) == "0o600"  # this user's, and nobody else's


def test_a_request_noted_again_under_its_identity_is_the_one_first_written(tmp_path: Path) -> None:
    """3C-2.6: a replay uses the installation and project the request was resolved to, not
    whatever it is replayed with."""
    file = tmp_path / "requests.jsonl"
    first = journal.note(file, ["approve", "G1"], "installation-a", "vat")

    again = journal.note(file, ["approve", "G1"], "installation-b", "another", first.request_id)

    assert again == first
    assert journal.read(file) == [first]


def test_a_line_cut_short_is_no_request_and_loses_no_other(tmp_path: Path) -> None:
    file = tmp_path / "requests.jsonl"
    first = journal.note(file, ["init"], "installation-a", None)
    with file.open("a") as cut_short:
        cut_short.write('{"request_id": "0199", "at": "2026')
        cut_short.write("\n")
    second = journal.note(file, ["passphrase"], "installation-a", None)

    assert journal.read(file) == [first, second]


def test_the_journal_is_in_the_users_state_directory_unless_told(tmp_path: Path) -> None:
    assert journal.journal_file({"HOME": "/home/po"}) == Path(
        "/home/po/.local/state/relay/requests.jsonl"
    )
    assert journal.journal_file({"HOME": "/home/po", "XDG_STATE_HOME": "/state"}) == Path(
        "/state/relay/requests.jsonl"
    )
    assert journal.journal_file({"RELAY_STATE_DIR": str(tmp_path)}) == tmp_path / "requests.jsonl"


def test_a_record_cut_short_with_no_line_end_does_not_take_the_next_request_with_it(
    tmp_path: Path,
) -> None:
    """3C-S1B-1.12: a write interrupted part-way leaves no newline. The next record is not
    joined to the fragment: the fragment is ended first."""
    file = tmp_path / "requests.jsonl"
    first = journal.note(file, ["init"], "installation-a", None)
    with file.open("a") as cut_short:
        cut_short.write('{"request_id": "0199", "at": "2026')

    second = journal.note(file, ["passphrase"], "installation-a", None)
    third = journal.note(file, ["policy", "state", "none"], "installation-a", None)

    assert journal.read(file) == [first, second, third]
