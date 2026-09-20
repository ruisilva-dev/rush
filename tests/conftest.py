from typing import AsyncGenerator
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool
from app.core.config import Settings

settings = Settings()


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(settings.postgres_url, poolclass=NullPool)

    async with engine.connect() as connection:
        async with connection.begin() as transaction:
            session_factory = async_sessionmaker(
                bind=connection,
                class_=AsyncSession,
                expire_on_commit=False,
            )
            async with session_factory() as session:
                yield session

            await transaction.rollback()

    await engine.dispose()
