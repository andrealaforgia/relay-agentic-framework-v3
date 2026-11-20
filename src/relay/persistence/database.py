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
    pool: Pool = AsyncConnectionPool(url, kwargs=kwargs, open=False, min_size=1, max_size=8)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


class MigrationDrift(Exception):
    """A migration that was already applied no longer has the content that was applied."""


async def migrate(pool: Pool) -> list[str]:
    """Apply every migration not yet recorded, in filename order. A recorded migration
    whose file has since changed is an error, never a silent skip."""
    applied: list[str] = []
    async with pool.connection() as connection, connection.transaction():
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version text PRIMARY KEY, checksum text NOT NULL)"
        )
        await connection.execute("LOCK TABLE schema_migrations IN EXCLUSIVE MODE")
        rows = await (await connection.execute("SELECT * FROM schema_migrations")).fetchall()
        recorded = {row["version"]: row["checksum"] for row in rows}
        migrations = resources.files("relay.persistence") / "migrations"
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
