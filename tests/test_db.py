from app.core.db import get_db
from sqlalchemy import text

import pytest


@pytest.mark.asyncio
async def test_db_connection() -> None:
    async for session in get_db():
        result = await session.scalar(text("SELECT 1"))

    assert result == 1
