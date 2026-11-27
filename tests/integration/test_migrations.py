"""An applied migration is history. Editing one must be noticed, not silently skipped."""

from __future__ import annotations

import asyncio
import contextvars
from pathlib import Path

import pytest
from psycopg_pool import AsyncConnectionPool

from relay.persistence.database import MigrationDrift, migrate


async def test_migrating_twice_applies_nothing_new(database: AsyncConnectionPool) -> None:
    assert await migrate(database) == []


async def test_an_applied_migration_whose_content_changed_is_an_error(
    database: AsyncConnectionPool,
) -> None:
    async with database.connection() as connection:
        await connection.execute(
            "UPDATE schema_migrations SET checksum = 'what-was-really-applied' "
            "WHERE version = '0001_foundation.sql'"
        )

    with pytest.raises(MigrationDrift, match="0001_foundation.sql"):
        await migrate(database)


async def test_an_applied_migration_missing_from_this_build_is_an_error(
    database: AsyncConnectionPool,
) -> None:
    async with database.connection() as connection:
        await connection.execute(
            "INSERT INTO schema_migrations (version, checksum) VALUES ('0000_gone.sql', 'x')"
        )

    with pytest.raises(MigrationDrift, match="0000_gone.sql"):
        await migrate(database)


# Every migration that has been pushed, by content. A build that has been pushed may have
# been run, and an installation made by it records these and refuses a build in which
# any of them reads differently. What comes after is a new migration, never an addition
# to one of these: a migration is added to this list in the commit that first pushes it.
SHIPPED = {
    "0001_foundation.sql": "09329c490ba244d5c797870679eaecbb8fd06c768614465f678c5968f1c5f3cf",
    "0002_executions.sql": "5eee91cb96afffead89a524e2a37b1a1a43045ad347bcaadc56fed424823ff1f",
    "0003_green_pairing.sql": "47fa13a9345a3ea0017db8583b451f35e10e4b61a11abfe1a17f8ba47bc00081",
    "0004_red_cycles.sql": "24277d5d5e7d00ff1c8cef2cc60e589f8656966bfca0d4917e26f2516a4eaf97",
    "0005_reviews.sql": "69ec14ca49b3fd2bca07b797165611794a000b7ed1aa21a94cd821cef62173f6",
    "0006_integration.sql": "85f61828eb4ae19397aed3e694f442e5340395bd3fcd9f5edb88153de14d6a4e",
    "0007_stories.sql": "b22edfbd9d9976e49ae4fb76d9a28dc30052af9c350f5f4ecec0a5928e76c889",
    "0008_closure.sql": "05f786930308dd6372a4af7c8a0b46455373f72323e69834a4428fe895504b46",
    "0009_assignments.sql": "23c256bd6ad8c673055946ba79abff9584cc168441229c9660712a5562ac4c1e",
    "0010_decisions.sql": "a7f46928a9a48b82760beac92af6fe07ffeb312592eb9837844c5dab814465dc",
    "0011_basis.sql": "5a63fa898479e191cbdb8c4c554b779be6342bc9aa91db4b7c523c7a1fd0c7a3",
    "0012_roadmap_revisions.sql": "be4da5393835b9629e3b21b25712239c96453e74f7db8ad63967d8a746ad41d6",
    "0013_obligations.sql": "67e658cb0fb43eb26ec4a0c617a663cb49606b42411c7f77b71f93074a17f71d",
    "0014_completion.sql": "78daa16d7862c9f07e6e637c3c1a67edfdd8a30ac5c09eb101e889bcce30d834",
    "0015_invocations.sql": "34cf81d4ffef9ced6a9bb51948f0c5d621564951b76a77f214e6a5c247654721",
    "0016_installation.sql": "11e9895fb1d31021ff63c82f5b413fa98264d5eba2f1b3aff9c2191409a0c389",
    "0017_enrolment.sql": "b69abf31a36f0792569da7ff3c0e6f2490b29076479e792c2a708221c6f75231",
    "0018_prepared.sql": "c45e8b0533564ba622bc13245be79bc42d0944b4af4194e495a2870679c8c79b",
}


def test_the_migrations_a_shipped_build_applied_read_as_they_did_then() -> None:
    """3C-S1B-1.4."""
    import hashlib
    from importlib import resources

    migrations = resources.files("relay.persistence") / "migrations"
    now = {
        script.name: hashlib.sha256(script.read_bytes()).hexdigest()
        for script in migrations.iterdir()
        if script.name in SHIPPED
    }

    assert now == SHIPPED


async def test_an_installation_made_by_the_last_build_keeps_what_it_is_across_the_upgrade(
    namespace: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3C-S1B-1.4: a database `relay up` of step 1a made opens under this build, which
    applies what is new and nothing else: the installation's identity, its term and its
    projects are as they were."""
    from importlib import resources

    from relay.application.leadership import Leadership
    from relay.persistence import database as database_module
    from relay.persistence.database import open_pool
    from tests.integration.conftest import DATABASE_URL

    real = resources.files("relay.persistence") / "migrations"
    as_shipped = tmp_path / "persistence" / "migrations"
    as_shipped.mkdir(parents=True)
    for script in real.iterdir():
        if script.name in SHIPPED:
            (as_shipped / script.name).write_bytes(script.read_bytes())

    class TheLastBuild:
        @staticmethod
        def files(_: str) -> Path:
            return tmp_path / "persistence"

    async with open_pool(DATABASE_URL) as admin, admin.connection() as connection:
        await connection.execute(f'CREATE SCHEMA "{namespace}"')
    try:
        async with open_pool(DATABASE_URL, schema=namespace) as pool:
            with monkeypatch.context() as then:
                then.setattr(database_module, "resources", TheLastBuild)
                assert sorted(await migrate(pool)) == sorted(SHIPPED)

            async def led() -> None:
                await (await Leadership.take(pool)).release()

            await asyncio.create_task(led(), context=contextvars.copy_context())
            async with pool.connection() as connection:
                await connection.execute(
                    "INSERT INTO projects (project_id, repository, trunk) VALUES ('vat', 'r', 'main')"
                )
                # What the last build left of the Product Owner's work: a passphrase, an
                # action prepared and not yet confirmed, and a decision with its receipt.
                await connection.execute("UPDATE installation SET passphrase = 'scrypt$kept'")
                await connection.execute(
                    "INSERT INTO prepared_actions (code, project_id, verb, target, why, shown, "
                    "basis, expires_at) VALUES ('c0de0001', 'vat', 'approve', 'a-gate', "
                    "'it looks right', 'shown', 'sha256:b', now() + interval '1 hour')"
                )
                await connection.execute(
                    "INSERT INTO human_actions (principal, request_id, target, action, payload, "
                    "digest, disposition, outcome) VALUES ('product-owner', 'r-1', 'vat', "
                    "'start', '{\"problem\": \"p\"}', 'd', 'effective', 'started F1')"
                )
                # As step 1a's processes had it: a statement, and nothing of who or when.
                await connection.execute(
                    "UPDATE installation SET policy = jsonb_build_object('statement', "
                    "'none applies, said in 1a'::text)"
                )
                before = await (
                    await connection.execute("SELECT installation_id, term FROM installation")
                ).fetchone()

            applied = await migrate(pool)

            assert applied and all(name not in SHIPPED for name in applied)
            async with pool.connection() as connection:
                after = await (
                    await connection.execute("SELECT installation_id, term FROM installation")
                ).fetchone()
                projects = await (
                    await connection.execute("SELECT project_id FROM projects")
                ).fetchall()
                await connection.execute(
                    "SELECT count(*) FROM boundary_requests"
                )  # and what is new is there
            assert after == before and before is not None and before["term"] == 1
            assert [each["project_id"] for each in projects] == ["vat"]
            # 3C-S2-2.1: what the last build left is there and can be used by this one.
            from relay.application import prepared
            from relay.cli.deciding import receipt_on_record

            kept = await prepared.find(pool, "c0de0001")
            assert kept is not None and (kept.consumed_by, kept.expired, kept.ended) == (
                None,
                False,
                None,
            )
            receipt = await receipt_on_record(pool, "r-1", "vat", "start", "", "p", None)
            assert receipt is not None and receipt.outcome == "started F1"
            async with pool.connection() as connection:
                which = await (
                    await connection.execute("SELECT passphrase_revision FROM installation")
                ).fetchone()
            assert which is not None and which["passphrase_revision"] == 1
            # 3C-S1B-2.8: the statement is kept and can be read, and what was never
            # kept of it is said to be unknown, not made up and not a crash.
            from relay.application import authority

            carried = await authority.policy_of(pool)
            assert carried is not None
            assert (carried.statement, carried.revision, carried.stated_by, carried.stated_at) == (
                "none applies, said in 1a",
                0,
                "",
                None,
            )
    finally:
        async with open_pool(DATABASE_URL) as admin, admin.connection() as connection:
            await connection.execute(f'DROP SCHEMA "{namespace}" CASCADE')


async def test_two_commands_setting_up_one_empty_database_at_once_both_succeed(
    namespace: str,
) -> None:
    """3C-S1B-3.5: nothing has been made in it, not even the table that records what has
    been. The first migration is one at a time from before its first statement."""
    from relay.persistence.database import open_pool
    from tests.integration.conftest import DATABASE_URL

    async with open_pool(DATABASE_URL) as admin, admin.connection() as connection:
        await connection.execute(f'CREATE SCHEMA "{namespace}"')
    try:
        async with (
            open_pool(DATABASE_URL, schema=namespace) as one,
            open_pool(DATABASE_URL, schema=namespace) as other,
        ):
            applied = await asyncio.gather(*(migrate(each) for each in (one, other, one, other)))

            assert sorted(len(each) for each in applied)[:3] == [0, 0, 0]
            assert max(len(each) for each in applied) > len(SHIPPED)
            async with one.connection() as connection:
                installations = await (
                    await connection.execute("SELECT count(*) AS n FROM installation")
                ).fetchone()
            assert installations is not None and installations["n"] == 1
    finally:
        async with open_pool(DATABASE_URL) as admin, admin.connection() as connection:
            await connection.execute(f'DROP SCHEMA "{namespace}" CASCADE')
