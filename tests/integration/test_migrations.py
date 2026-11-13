from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import AsyncEngine

from fraud.db.models import Base


async def test_migrations_match_models(engine: AsyncEngine) -> None:
    """Se alguém mudar um modelo e esquecer a migração, este teste falha."""

    def diff(connection):  # type: ignore[no-untyped-def]
        return compare_metadata(MigrationContext.configure(connection), Base.metadata)

    async with engine.connect() as conn:
        differences = await conn.run_sync(diff)
    assert differences == []
