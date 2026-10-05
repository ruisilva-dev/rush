import pytest
import asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from app.models.drop import Drop, DropStatus
from app.models.drop_unit import DropUnit, DropUnitStatus


@pytest.mark.asyncio
async def test_purchase_naive_success_uncontended(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # Seed drop and unit
    past_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    drop = Drop(
        name="Single Unit Drop",
        status=DropStatus.ACTIVE,
        total_units=1,
        scheduled_at=past_time,
    )
    db_session.add(drop)
    await db_session.flush()

    unit = DropUnit(drop_id=drop.id, status=DropUnitStatus.AVAILABLE)
    db_session.add(unit)
    await db_session.flush()

    # Send request for the unit
    buyer_id = uuid4()
    response = await client.post(
        f"/drops/{drop.id}/purchase-naive",
        json={"user_id": str(buyer_id)},
    )

    # Verify unit was acquired
    assert response.status_code == 200
    data = response.json()
    assert data["unit_id"] == str(unit.id)
    assert data["drop_id"] == str(drop.id)
    assert data["user_id"] == str(buyer_id)
    assert data["status"] == DropUnitStatus.PURCHASED

    # Verify database state
    await db_session.refresh(unit)
    assert unit.status == DropUnitStatus.PURCHASED
    assert unit.held_by_user_id == buyer_id


@pytest.mark.asyncio
async def test_purchase_naive_not_yet_open_future_schedule(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    future_time = datetime.now(timezone.utc) + timedelta(days=1)
    drop = Drop(
        name="Future Drop",
        status=DropStatus.SCHEDULED,
        total_units=5,
        scheduled_at=future_time,
    )
    db_session.add(drop)
    await db_session.flush()

    response = await client.post(
        f"/drops/{drop.id}/purchase-naive",
        json={"user_id": str(uuid4())},
    )

    assert response.status_code == 409
    data = response.json()
    assert data["detail"]["code"] == "not_yet_open"


@pytest.mark.asyncio
async def test_purchase_naive_not_yet_open_draft(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    drop = Drop(
        name="Draft Drop",
        status=DropStatus.DRAFT,
        total_units=5,
        scheduled_at=None,
    )
    db_session.add(drop)
    await db_session.flush()

    response = await client.post(
        f"/drops/{drop.id}/purchase-naive",
        json={"user_id": str(uuid4())},
    )

    assert response.status_code == 409
    data = response.json()
    assert data["detail"]["code"] == "not_yet_open"


@pytest.mark.asyncio
async def test_purchase_naive_sold_out(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    past_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    drop = Drop(
        name="Sold Out Drop",
        status=DropStatus.ACTIVE,
        total_units=1,
        scheduled_at=past_time,
    )
    db_session.add(drop)
    await db_session.flush()

    # Seed an already purchased unit
    unit = DropUnit(
        drop_id=drop.id,
        status=DropUnitStatus.PURCHASED,
        held_by_user_id=uuid4(),
    )
    db_session.add(unit)
    await db_session.flush()

    response = await client.post(
        f"/drops/{drop.id}/purchase-naive",
        json={"user_id": str(uuid4())},
    )

    assert response.status_code == 409
    data = response.json()
    assert data["detail"]["code"] == "sold_out"


@pytest.mark.asyncio
async def test_purchase_naive_route_errors(client: AsyncClient) -> None:
    # 404 for non-existent drop
    res_404 = await client.post(
        f"/drops/{uuid4()}/purchase-naive",
        json={"user_id": str(uuid4())},
    )
    assert res_404.status_code == 404

    # 422 for malformed drop UUID in URL
    res_malformed_url = await client.post(
        "/drops/not-a-valid-uuid/purchase-naive",
        json={"user_id": str(uuid4())},
    )
    assert res_malformed_url.status_code == 422

    # 422 for malformed user_id in payload
    res_malformed_body = await client.post(
        f"/drops/{uuid4()}/purchase-naive",
        json={"user_id": "invalid-user-uuid"},
    )
    assert res_malformed_body.status_code == 422

    # 422 for missing user_id in payload
    res_empty_body = await client.post(
        f"/drops/{uuid4()}/purchase-naive",
        json={},
    )
    assert res_empty_body.status_code == 422


# NOTE: inherently non-deterministic — depends on both requests' SELECTs
# interleaving before either commits. Near-certain under async I/O with no
# artificial delay, but not guaranteed. A rare flake here means the race
# window didn't open that run, not that the endpoint became correct.
@pytest.mark.asyncio
async def test_purchase_naive_race_condition_silent_oversell(
    committed_session: AsyncSession, concurrent_client: AsyncClient
) -> None:
    past_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    drop = Drop(
        name="Single Unit Drop",
        status=DropStatus.ACTIVE,
        total_units=1,
        scheduled_at=past_time,
    )
    committed_session.add(drop)
    await committed_session.flush()

    unit = DropUnit(drop_id=drop.id, status=DropUnitStatus.AVAILABLE)
    committed_session.add(unit)
    await committed_session.commit()

    user_a = str(uuid4())
    user_b = str(uuid4())
    url = f"/drops/{drop.id}/purchase-naive"

    res_a, res_b = await asyncio.gather(
        concurrent_client.post(url, json={"user_id": user_a}),
        concurrent_client.post(url, json={"user_id": user_b}),
    )

    assert res_a.json()["unit_id"] == res_b.json()["unit_id"] == str(unit.id)
    assert res_a.status_code == 200
    assert res_b.status_code == 200

    await committed_session.refresh(unit)
    assert unit.status == DropUnitStatus.PURCHASED
    assert str(unit.held_by_user_id) in {user_a, user_b}
