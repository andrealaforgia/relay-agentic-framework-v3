"""PostgreSQL is the authority for workflow state and the event ledger."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import resources
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

type Connection = AsyncConnection[dict[str, Any]]
type Pool = AsyncConnectionPool[Connection]


@asynccontextmanager
async def open_pool(url: str, schema: str | None = None) -> AsyncIterator[Pool]:
    kwargs: dict[str, Any] = {"row_factory": dict_row}
    if schema is not None:
        kwargs["options"] = f"-c search_path={schema}"
        # Shows in pg_stat_activity, so whoever is looking at a busy or blocked
        # connection can tell which Relay it belongs to.
        kwargs["application_name"] = f"relay:{schema}"
    pool: Pool = AsyncConnectionPool(url, kwargs=kwargs, open=False, min_size=1, max_size=8)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


# A migration that was once pushed with other content, by mistake, and may have been
# applied in that form. An installation that recorded that exact content is brought to
# this build by name: the content it applied already holds what the migrations listed
# beside it add, so those are recorded as applied and are not run. This is for the
# listed checksums and for nothing else: any other difference is still drift.
#   0018 as pushed in 5f6da3d held, besides the table, the three additions that are 0019.
SUPERSEDED = {
    "0018_prepared.sql": {
        "f0d231f0075b53b3624c44ef53f16b3444ac4d3c366165cd0fefccecf03081eb": (
            "0019_confirmation.sql",
        )
    }
}


class MigrationDrift(Exception):
    """A migration that was already applied no longer has the content that was applied."""


async def migrate(pool: Pool) -> list[str]:
    """Apply every migration not yet recorded, in filename order. A recorded migration
    whose file has since changed is an error, never a silent skip."""
    applied: list[str] = []
    async with pool.connection() as connection, connection.transaction():
        # Before the first statement that makes anything: two commands on a database
        # nothing has been made in would otherwise both set about making its first table,
        # and PostgreSQL may refuse one of them. Of this schema's, so that installations
        # in other schemas of one database do not wait on each other.
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('relay migrations of ' || "
            "current_schema(), 0))"
        )
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version text PRIMARY KEY, checksum text NOT NULL)"
        )
        await connection.execute("LOCK TABLE schema_migrations IN EXCLUSIVE MODE")
        rows = await (await connection.execute("SELECT * FROM schema_migrations")).fetchall()
        recorded = {row["version"]: row["checksum"] for row in rows}
        migrations = resources.files("relay.persistence") / "migrations"
        present = {script.name for script in migrations.iterdir()}
        if missing := sorted(set(recorded) - present):
            raise MigrationDrift(f"applied migrations are missing from this build: {missing}")
        for name, known in SUPERSEDED.items():
            if name in recorded and recorded[name] in known:
                # Applied in a form that already holds what these add: said to be
                # applied, as they are, and the record brought to this build's content.
                script = migrations / name
                now = hashlib.sha256(script.read_bytes()).hexdigest()
                for held in known[recorded[name]]:
                    if held not in recorded:
                        content = (migrations / held).read_bytes()
                        checksum = hashlib.sha256(content).hexdigest()
                        await connection.execute(
                            "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                            (held, checksum),
                        )
                        recorded[held] = checksum
                await connection.execute(
                    "UPDATE schema_migrations SET checksum = %s WHERE version = %s", (now, name)
                )
                recorded[name] = now
        for script in sorted(migrations.iterdir(), key=lambda entry: entry.name):
            if not script.name.endswith(".sql"):
                continue
            content = script.read_bytes()
            checksum = hashlib.sha256(content).hexdigest()
            if script.name in recorded:
                if recorded[script.name] != checksum:
                    raise MigrationDrift(
                        f"{script.name} was applied with different content; "
                        "write a new migration instead of editing an applied one"
                    )
                continue
            await connection.execute(content)
            await connection.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                (script.name, checksum),
            )
            applied.append(script.name)
    return applied


async def schema_state(pool: Pool) -> tuple[list[str], list[str]]:
    """What a migration would do, without doing it: the migrations this build has that
    the database has not had, and those the database had with other content. Reads only.
    A database Relay has never touched has had none."""
    migrations = resources.files("relay.persistence") / "migrations"
    scripts = {
        script.name: hashlib.sha256(script.read_bytes()).hexdigest()
        for script in migrations.iterdir()
        if script.name.endswith(".sql")
    }
    async with pool.connection() as connection:
        there = await (
            await connection.execute("SELECT to_regclass('schema_migrations') AS there")
        ).fetchone()
        recorded: dict[str, str] = {}
        if there is not None and there["there"] is not None:
            rows = await (await connection.execute("SELECT * FROM schema_migrations")).fetchall()
            recorded = {row["version"]: row["checksum"] for row in rows}
    pending = sorted(name for name in scripts if name not in recorded)
    drifted = sorted(
        name for name, checksum in recorded.items() if scripts.get(name, checksum) != checksum
    ) + sorted(name for name in recorded if name not in scripts)
    return pending, drifted
