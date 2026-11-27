"""The enrolment commands, run as a person runs them: from a shell, at a terminal."""

from __future__ import annotations

from pathlib import Path

from psycopg_pool import AsyncConnectionPool

from relay.application import authority
from relay.cli import journal
from tests.integration.person import at_a_terminal, with_no_terminal
from tests.integration.test_enrolment import IMAGE, PASSPHRASE, checkout_of


def init(tmp_path: Path) -> list[str]:
    root = tmp_path.resolve()
    return ["init", "--workspaces", str(root / "w"), "--mirrors", str(root / "m"), "--image", IMAGE]


async def test_a_person_enrols_states_the_policy_and_changes_the_passphrase_at_a_terminal(
    database: AsyncConnectionPool, shell: dict[str, str], tmp_path: Path
) -> None:
    here = checkout_of(tmp_path / "remote.git", tmp_path / "vat")

    enrolled = at_a_terminal(init(tmp_path), [PASSPHRASE, PASSPHRASE], shell, here)

    assert enrolled.code == 0, enrolled.printed
    assert "project vat: registered" in enrolled.printed
    assert "the passphrase: set" in enrolled.printed
    assert PASSPHRASE not in enrolled.shown + enrolled.printed  # typed, and shown nowhere
    await authority.require(database, PASSPHRASE)

    stated = at_a_terminal(
        ["policy", "state", "no managed policy applies to this host"], [PASSPHRASE], shell, here
    )

    assert stated.code == 0, stated.printed
    assert "revision 1" in stated.printed
    # What is being stated is shown on the terminal the passphrase is then read from.
    assert "no managed policy applies to this host" in stated.shown
    assert stated.shown.index("no managed policy") < stated.shown.index("passphrase:")
    policy = await authority.policy_of(database)
    assert policy is not None and policy.statement == "no managed policy applies to this host"
    assert "no managed policy applies" in at_a_terminal(["policy", "show"], [], shell, here).printed

    changed = at_a_terminal(
        ["passphrase"], [PASSPHRASE, "a new one altogether", "a new one altogether"], shell, here
    )

    assert changed.code == 0, changed.printed
    await authority.require(database, "a new one altogether")

    # Each command that acted left a handle, and said it first.
    noted = journal.read(Path(shell["RELAY_STATE_DIR"]) / "requests.jsonl")
    assert [each.command[0] for each in noted] == ["init", "policy", "passphrase"]
    assert enrolled.printed.startswith(f"request {noted[0].request_id}")
    listed = at_a_terminal(["requests"], [], shell, here).printed
    assert all(each.request_id in listed for each in noted)


async def test_what_a_model_can_run_decides_nothing(
    database: AsyncConnectionPool, shell: dict[str, str], tmp_path: Path
) -> None:
    """S3-1.8: with no controlling terminal a deciding command is refused, whatever is on
    its standard input or in its environment; and at a terminal, without the passphrase."""
    here = checkout_of(tmp_path / "remote.git", tmp_path / "vat")
    assert at_a_terminal(init(tmp_path), [PASSPHRASE, PASSPHRASE], shell, here).code == 0
    as_a_model = {**shell, "RELAY_PASSPHRASE": PASSPHRASE}

    stated = with_no_terminal(
        ["policy", "state", "whatever suits"], as_a_model, here, given=PASSPHRASE + "\n"
    )
    changed = with_no_terminal(
        ["passphrase"], as_a_model, here, given=f"{PASSPHRASE}\nmine now ok\nmine now ok\n"
    )
    guessed = at_a_terminal(["policy", "state", "whatever suits"], ["a guess at it"], shell, here)

    assert stated.code == 1 and "needs a person at a terminal" in stated.printed
    assert changed.code == 1 and "needs a person at a terminal" in changed.printed
    assert guessed.code == 1 and "not the passphrase" in guessed.printed
    assert await authority.policy_of(database) is None
    await authority.require(database, PASSPHRASE)


async def test_enrolment_with_no_terminal_sets_no_passphrase_and_says_why(
    database: AsyncConnectionPool, shell: dict[str, str], tmp_path: Path
) -> None:
    here = checkout_of(tmp_path / "remote.git", tmp_path / "vat")

    enrolled = with_no_terminal(init(tmp_path), shell, here, given=f"{PASSPHRASE}\n{PASSPHRASE}\n")

    assert enrolled.code == 1 and "needs a person at a terminal" in enrolled.printed
    assert not await authority.is_set(database)


async def test_an_address_with_a_secret_in_it_is_refused_and_kept_nowhere(
    database: AsyncConnectionPool, shell: dict[str, str], tmp_path: Path
) -> None:
    """3C-S1B-1.7: not in the journal, and not in anything the command says."""
    here = checkout_of(tmp_path / "remote.git", tmp_path / "vat")
    secret = "hunter2-in-the-address"
    address = f"https://someone:{secret}@example.invalid/vat.git"

    refused = with_no_terminal([*init(tmp_path), "--repository", address], shell, here)

    assert refused.code == 1 and "cannot be used" in refused.printed
    assert secret not in refused.printed
    file = Path(shell["RELAY_STATE_DIR"]) / "requests.jsonl"
    assert not file.exists() or secret not in file.read_text()
    assert secret not in with_no_terminal(["requests"], shell, here).printed


async def test_a_request_that_changed_the_installation_is_repeated_by_its_handle_and_changes_nothing(
    database: AsyncConnectionPool, shell: dict[str, str], tmp_path: Path
) -> None:
    """3C-S1B-1.11: the reply was lost. The handle that was printed first says what the
    request did, to anyone, at no terminal, and does it no second time."""
    here = checkout_of(tmp_path / "remote.git", tmp_path / "vat")
    assert at_a_terminal(init(tmp_path), [PASSPHRASE, PASSPHRASE], shell, here).code == 0
    stated = at_a_terminal(["policy", "state", "none applies here"], [PASSPHRASE], shell, here)
    handle = stated.printed.split()[1]

    again = with_no_terminal(
        ["policy", "state", "none applies here", "--request-id", handle], shell, here
    )
    another = with_no_terminal(
        ["policy", "state", "something else", "--request-id", handle], shell, here
    )

    assert again.code == 0 and "revision 1" in again.printed and "as on record" in again.printed
    assert another.code == 1 and "for something else" in another.printed
    policy = await authority.policy_of(database)
    assert policy is not None and (policy.revision, policy.statement) == (1, "none applies here")

    changed = at_a_terminal(
        ["passphrase"], [PASSPHRASE, "a new one altogether", "a new one altogether"], shell, here
    )
    replayed = with_no_terminal(
        ["passphrase", "--request-id", changed.printed.split()[1]], shell, here
    )
    assert replayed.code == 0 and "as on record" in replayed.printed
    await authority.require(database, "a new one altogether")


async def test_an_address_given_and_then_given_again_leaves_no_secret_in_the_journal(
    database: AsyncConnectionPool, shell: dict[str, str], tmp_path: Path
) -> None:
    """3C-S1B-2.9: a wrapper supplied one address and the caller another. The last is the
    one used and checked; the journal has what was understood, not the words as typed."""
    here = checkout_of(tmp_path / "remote.git", tmp_path / "vat")
    secret = "hunter2-in-the-first-address"
    twice = [
        *init(tmp_path),
        "--repository",
        f"https://someone:{secret}@example.invalid/vat.git",
        "--repository",
        str(tmp_path / "remote.git"),
    ]

    enrolled = at_a_terminal(twice, [PASSPHRASE, PASSPHRASE], shell, here)

    assert enrolled.code == 0, enrolled.printed
    file = Path(shell["RELAY_STATE_DIR"]) / "requests.jsonl"
    assert secret not in file.read_text() and str(tmp_path / "remote.git") in file.read_text()
    assert secret not in with_no_terminal(["requests"], shell, here).printed
    assert secret not in enrolled.printed + enrolled.shown
