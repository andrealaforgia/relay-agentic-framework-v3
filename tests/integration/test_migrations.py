"""An applied migration is history. Editing one must be noticed, not silently skipped."""

from __future__ import annotations

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
