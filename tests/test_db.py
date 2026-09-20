from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
import pytest


@pytest.mark.asyncio
async def test_db_connection(db_session: AsyncSession) -> None:
    result = await db_session.scalar(text("SELECT 1"))
    assert result == 1


@pytest.mark.asyncio
async def test_db_schema(db_session: AsyncSession) -> None:
    result = await db_session.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'drop_units';"
        )
    )
    indexes = {row.indexname: row.indexdef for row in result.all()}

    # Standard foreign key index exists
    assert "ix_drop_units_drop_id" in indexes

    # Partial allocation index exists and contains the status filter
    assert "ix_drop_units_drop_id_available" in indexes
    assert "status = 'available'" in indexes["ix_drop_units_drop_id_available"]


@pytest.mark.asyncio
async def test_tables_exist(db_session: AsyncSession) -> None:
    result = await db_session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public';"
        )
    )
    tables = {row[0] for row in result.all()}

    assert "drops" in tables
    assert "drop_units" in tables
