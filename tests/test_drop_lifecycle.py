import asyncio
import pytest
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.drop import Drop, DropStatus
from app.models.drop_unit import DropUnit, DropUnitStatus

ENDPOINTS = ["purchase-naive", "purchase-pessimistic"]


async def _seed_drop(
    session: AsyncSession, unit_count: int, drop_status: DropStatus
) -> Drop:
    drop = Drop(
        name="Lifecycle Drop",
        status=drop_status,
        total_units=unit_count,
        scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    session.add(drop)
    await session.flush()
    session.add_all(
        DropUnit(drop_id=drop.id, status=DropUnitStatus.AVAILABLE)
        for _ in range(unit_count)
    )
    await session.flush()
    return drop


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_purchase_activates_scheduled_drop_then_completes(
    client: AsyncClient, db_session: AsyncSession, endpoint: str
) -> None:
    drop = await _seed_drop(db_session, 2, DropStatus.SCHEDULED)
    url = f"/drops/{drop.id}/{endpoint}"

    res = await client.post(url, json={"user_id": str(uuid4())})
    assert res.status_code == 200
    await db_session.refresh(drop)
    assert drop.status == DropStatus.ACTIVE

    res = await client.post(url, json={"user_id": str(uuid4())})
    assert res.status_code == 200
    await db_session.refresh(drop)
    assert drop.status == DropStatus.COMPLETED


@pytest.mark.asyncio
async def test_purchase_pessimistic_concurrent_last_units_complete_drop(
    committed_session: AsyncSession, concurrent_client: AsyncClient
) -> None:
    drop = await _seed_drop(committed_session, 2, DropStatus.SCHEDULED)
    await committed_session.commit()
    url = f"/drops/{drop.id}/purchase-pessimistic"

    responses = await asyncio.gather(
        *(concurrent_client.post(url, json={"user_id": str(uuid4())}) for _ in range(2))
    )

    assert all(r.status_code == 200 for r in responses)
    status = await committed_session.scalar(
        select(Drop.status)
        .where(Drop.id == drop.id)
        .execution_options(populate_existing=True)
    )
    assert status == DropStatus.COMPLETED
