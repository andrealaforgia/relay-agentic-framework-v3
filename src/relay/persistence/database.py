"""PostgreSQL is the authority for workflow state and the event ledger."""

from __future__ import annotations

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


async def migrate(pool: Pool) -> list[str]:
    """Apply every migration not yet recorded, in filename order."""
    applied: list[str] = []
    async with pool.connection() as connection, connection.transaction():
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY)"
        )
        await connection.execute("LOCK TABLE schema_migrations IN EXCLUSIVE MODE")
        rows = await (await connection.execute("SELECT version FROM schema_migrations")).fetchall()
        done = {row["version"] for row in rows}
        migrations = resources.files("relay.persistence") / "migrations"
        for script in sorted(migrations.iterdir(), key=lambda entry: entry.name):
            if not script.name.endswith(".sql") or script.name in done:
                continue
            await connection.execute(script.read_text().encode())
            await connection.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)", (script.name,)
            )
            applied.append(script.name)
    return applied
