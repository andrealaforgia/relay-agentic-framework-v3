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
