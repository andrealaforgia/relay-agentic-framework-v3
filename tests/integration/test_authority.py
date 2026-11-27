"""The Product Owner's passphrase, and the operator's statement of policy."""

from __future__ import annotations

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.application import authority
from relay.application.authority import Refused
from relay.runtime import stated_policy

PASSPHRASE = "correct horse battery"


def test_what_is_kept_of_a_passphrase_is_a_salted_hash_that_only_it_matches() -> None:
    kept, again = authority.hashed(PASSPHRASE), authority.hashed(PASSPHRASE)

    assert PASSPHRASE not in kept and kept != again  # salted: the same phrase, another hash
    assert authority.matches(PASSPHRASE, kept) and authority.matches(PASSPHRASE, again)
    assert not authority.matches(PASSPHRASE + " ", kept)
    assert not authority.matches(PASSPHRASE, "not a hash at all")
    assert not authority.matches(PASSPHRASE, kept.replace("scrypt", "plain"))


async def test_the_passphrase_is_set_once_at_enrolment_and_a_second_enrolment_leaves_it(
    database: AsyncConnectionPool,
) -> None:
    assert not await authority.is_set(database)
    with pytest.raises(Refused, match="no passphrase has been set"):
        await authority.require(database, PASSPHRASE)
    with pytest.raises(Refused, match="at least"):
        await authority.set_first(database, "short")

    assert await authority.set_first(database, PASSPHRASE)
    assert not await authority.set_first(database, "another one entirely")

    assert await authority.is_set(database)
    await authority.require(database, PASSPHRASE)
    with pytest.raises(Refused, match="not the passphrase"):
        await authority.require(database, "another one entirely")
    async with database.connection() as connection:
        row = await (await connection.execute("SELECT passphrase FROM installation")).fetchone()
    assert row is not None and PASSPHRASE not in row["passphrase"]


async def test_the_passphrase_is_changed_only_by_whoever_knows_it(
    database: AsyncConnectionPool,
) -> None:
    await authority.set_first(database, PASSPHRASE)

    with pytest.raises(Refused, match="not the passphrase"):
        await authority.change(database, "a guess at it", "what a model would like")
    with pytest.raises(Refused, match="at least"):
        await authority.change(database, PASSPHRASE, "short")
    await authority.require(database, PASSPHRASE)  # unchanged by either

    await authority.change(database, PASSPHRASE, "a new one altogether")

    await authority.require(database, "a new one altogether")
    with pytest.raises(Refused):
        await authority.require(database, PASSPHRASE)


async def test_a_statement_of_policy_is_the_product_owners_and_each_is_a_revision_on_record(
    database: AsyncConnectionPool,
) -> None:
    """D-10 and 3C-1.5: not an editable field. Stated with the passphrase, with who and
    when, each statement a new revision and the earlier ones kept."""
    await authority.set_first(database, PASSPHRASE)
    assert await authority.policy_of(database) is None

    with pytest.raises(Refused, match="not the passphrase"):
        await authority.state_policy(database, "a guess", "none is applied", "operator")
    with pytest.raises(Refused, match="says something"):
        await authority.state_policy(database, PASSPHRASE, "  ", "operator")
    assert await authority.policy_of(database) is None

    first = await authority.state_policy(database, PASSPHRASE, " none is applied ", "andrea")
    second = await authority.state_policy(database, PASSPHRASE, "managed settings v7", "andrea")

    assert (first.statement, first.revision, first.stated_by) == ("none is applied", 1, "andrea")
    assert (second.statement, second.revision) == ("managed settings v7", 2)
    assert second.stated_at >= first.stated_at
    assert await authority.policy_of(database) == second
    async with database.connection() as connection:
        # What a worker reads where it admits a call is the statement in force.
        assert await stated_policy(connection) == "managed settings v7"
        row = await (
            await connection.execute(
                "SELECT jsonb_agg(e->>'statement') AS earlier FROM installation, "
                "jsonb_array_elements(policy->'earlier') e"
            )
        ).fetchone()
    assert row is not None and row["earlier"] == ["none is applied"]


async def test_a_passphrase_changed_a_moment_ago_authorises_no_statement_of_policy(
    database: AsyncConnectionPool,
) -> None:
    """3C-S1B-1.5: the rotation holds the installation's row. A statement made with the
    old passphrase waits for it, and is then checked against the one there is."""
    import asyncio

    await authority.set_first(database, PASSPHRASE)
    async with database.connection() as rotating, rotating.transaction():
        await rotating.execute("SELECT 1 FROM installation FOR UPDATE")
        stating = asyncio.create_task(
            authority.state_policy(database, PASSPHRASE, "whatever suits", "a model")
        )
        await asyncio.sleep(0.5)
        assert not stating.done()
        await rotating.execute(
            "UPDATE installation SET passphrase = %s", (authority.hashed("a new one altogether"),)
        )

    with pytest.raises(Refused, match="not the passphrase"):
        await stating
    assert await authority.policy_of(database) is None


async def test_a_request_that_stated_the_policy_or_changed_the_passphrase_does_it_once(
    database: AsyncConnectionPool,
) -> None:
    """3C-S1B-1.11: the reply may be lost after the change has committed. Sent again
    under its identity the request is answered from its receipt: no second revision, and
    no refusal of a rotation that in fact succeeded."""
    await authority.set_first(database, PASSPHRASE)

    first = await authority.state_policy(database, PASSPHRASE, "none applies", "andrea", "r-1")
    again = await authority.state_policy(database, "not even needed", "none applies", "x", "r-1")

    assert again == first and first.revision == 1
    assert (await authority.policy_of(database)) == first
    assert (await authority.receipt_of(database, "r-1") or {})["revision"] == 1
    with pytest.raises(Refused, match="for something else"):
        await authority.state_policy(database, PASSPHRASE, "another statement", "andrea", "r-1")
    assert (
        await authority.state_policy(database, PASSPHRASE, "none applies", "a", "r-2")
    ).revision == 2

    assert await authority.change(database, PASSPHRASE, "a new one altogether", "r-3") == "changed"
    # Asked again with the old passphrase, as someone who never heard the reply would.
    assert (
        await authority.change(database, PASSPHRASE, "a new one altogether", "r-3")
        == "changed, as on record"
    )
    await authority.require(database, "a new one altogether")
    kept = await authority.receipt_of(database, "r-3")
    assert kept == {"action": "passphrase", "changed": True}  # and nothing of either secret
    with pytest.raises(Refused, match="for something else"):
        await authority.state_policy(database, "a new one altogether", "x", "y", "r-3")


async def test_a_handle_is_taken_once_for_one_action_asking_one_thing(
    database: AsyncConnectionPool,
) -> None:
    """3C-S1B-3.3 and 3.4: in the database, before a command acts or asks anybody
    anything. Whichever command takes it first has it; any other use of it is refused,
    from whatever machine, and a statement cannot be swapped under a handle already
    taken for another."""
    assert await authority.take_handle(database, "h-1", "policy", "none applies")
    assert not await authority.take_handle(database, "h-1", "policy", "none applies")

    with pytest.raises(Refused, match=r"something else \(`relay policy`\)"):
        await authority.take_handle(database, "h-1", "doctor", "boundary runs, retrying none")
    with pytest.raises(Refused, match="a request for something else:"):
        await authority.take_handle(database, "h-1", "policy", "another statement")
