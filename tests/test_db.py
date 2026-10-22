import asyncio
from unittest.mock import AsyncMock, patch
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
import pytest
from app.core.db import get_db


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


@pytest.mark.asyncio
async def test_get_db_cancelled_error_rollback() -> None:
    session = AsyncMock(spec=AsyncSession)
    session.__aenter__.return_value = session

    with patch("app.core.db.AsyncSessionLocal", return_value=session):
        gen = get_db()
        await gen.__anext__()

        with pytest.raises(asyncio.CancelledError):
            await gen.athrow(asyncio.CancelledError())

    session.rollback.assert_awaited_once()
