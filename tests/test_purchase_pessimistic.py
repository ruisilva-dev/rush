import pytest
import asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from app.models.drop import Drop, DropStatus
from app.models.drop_unit import DropUnit, DropUnitStatus


@pytest.mark.asyncio
async def test_purchase_pessimistic_success_uncontended(
    client: AsyncClient, db_session: AsyncSession
) -> None:
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

    buyer_id = uuid4()
    response = await client.post(
        f"/drops/{drop.id}/purchase-pessimistic",
        json={"user_id": str(buyer_id)},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["unit_id"] == str(unit.id)
    assert data["drop_id"] == str(drop.id)
    assert data["user_id"] == str(buyer_id)
    assert data["status"] == DropUnitStatus.PURCHASED

    await db_session.refresh(unit)
    assert unit.status == DropUnitStatus.PURCHASED
    assert unit.held_by_user_id == buyer_id


@pytest.mark.asyncio
async def test_purchase_pessimistic_not_yet_open_future_schedule(
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
        f"/drops/{drop.id}/purchase-pessimistic",
        json={"user_id": str(uuid4())},
    )

    assert response.status_code == 409
    data = response.json()
    assert data["detail"]["code"] == "not_yet_open"


@pytest.mark.asyncio
async def test_purchase_pessimistic_not_yet_open_draft(
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
        f"/drops/{drop.id}/purchase-pessimistic",
        json={"user_id": str(uuid4())},
    )

    assert response.status_code == 409
    data = response.json()
    assert data["detail"]["code"] == "not_yet_open"


@pytest.mark.asyncio
async def test_purchase_pessimistic_sold_out(
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

    unit = DropUnit(
        drop_id=drop.id,
        status=DropUnitStatus.PURCHASED,
        held_by_user_id=uuid4(),
    )
    db_session.add(unit)
    await db_session.flush()

    response = await client.post(
        f"/drops/{drop.id}/purchase-pessimistic",
        json={"user_id": str(uuid4())},
    )

    assert response.status_code == 409
    data = response.json()
    assert data["detail"]["code"] == "sold_out"


@pytest.mark.asyncio
async def test_purchase_pessimistic_route_errors(client: AsyncClient) -> None:
    res_404 = await client.post(
        f"/drops/{uuid4()}/purchase-pessimistic",
        json={"user_id": str(uuid4())},
    )
    assert res_404.status_code == 404

    res_malformed_url = await client.post(
        "/drops/not-a-valid-uuid/purchase-pessimistic",
        json={"user_id": str(uuid4())},
    )
    assert res_malformed_url.status_code == 422

    res_malformed_body = await client.post(
        f"/drops/{uuid4()}/purchase-pessimistic",
        json={"user_id": "invalid-user-uuid"},
    )
    assert res_malformed_body.status_code == 422

    res_empty_body = await client.post(
        f"/drops/{uuid4()}/purchase-pessimistic",
        json={},
    )
    assert res_empty_body.status_code == 422


@pytest.mark.asyncio
async def test_purchase_pessimistic_solve_silent_oversell(
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
    url = f"/drops/{drop.id}/purchase-pessimistic"

    res_a, res_b = await asyncio.gather(
        concurrent_client.post(url, json={"user_id": user_a}),
        concurrent_client.post(url, json={"user_id": user_b}),
    )

    responses = [res_a, res_b]
    successes = [r for r in responses if r.status_code == 200]
    conflicts = [r for r in responses if r.status_code == 409]

    assert len(successes) == len(conflicts) == 1

    assert conflicts[0].json()["detail"]["code"] == "sold_out"

    await committed_session.refresh(unit)
    assert unit.status == DropUnitStatus.PURCHASED
    assert str(unit.held_by_user_id) in {user_a, user_b}
    assert successes[0].json()["user_id"] == str(unit.held_by_user_id)


@pytest.mark.asyncio
async def test_purchase_pessimistic_lock_held_deterministic(
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

    unit_id = unit.id

    # Open uncommitted transaction holding a FOR UPDATE lock on the unit
    lock_stmt = select(DropUnit).where(DropUnit.id == unit.id).with_for_update()
    locked_unit = await committed_session.scalar(lock_stmt)
    assert locked_unit is not None

    user = str(uuid4())
    url = f"/drops/{drop.id}/purchase-pessimistic"

    # Request while lock is held
    res_blocked = await concurrent_client.post(url, json={"user_id": user})
    assert res_blocked.status_code == 409
    assert res_blocked.json()["detail"]["code"] == "sold_out"

    # Release lock
    await committed_session.rollback()

    # Retry request
    res_success = await concurrent_client.post(url, json={"user_id": user})
    assert res_success.status_code == 200
    assert res_success.json()["unit_id"] == str(unit_id)
    assert res_success.json()["status"] == DropUnitStatus.PURCHASED

    # Verify final database state
    await committed_session.refresh(unit)
    assert unit.status == DropUnitStatus.PURCHASED
    assert str(unit.held_by_user_id) == user


@pytest.mark.asyncio
async def test_purchase_pessimistic_multi_unit_contention(
    committed_session: AsyncSession, concurrent_client: AsyncClient
) -> None:
    unit_count = 5
    units = []

    past_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    drop = Drop(
        name="Multi Unit Drop",
        status=DropStatus.ACTIVE,
        total_units=unit_count,
        scheduled_at=past_time,
    )
    committed_session.add(drop)
    await committed_session.flush()

    for _ in range(unit_count):
        unit = DropUnit(drop_id=drop.id, status=DropUnitStatus.AVAILABLE)
        committed_session.add(unit)
        units.append(unit)
    await committed_session.commit()

    expected_unit_ids = {str(u.id) for u in units}
    json_payloads = [{"user_id": str(uuid4())} for _ in range(unit_count)]
    url = f"/drops/{drop.id}/purchase-pessimistic"

    tasks = [concurrent_client.post(url, json=payload) for payload in json_payloads]

    responses = await asyncio.gather(*tasks)

    assert len(responses) == unit_count
    assert all(response.status_code == 200 for response in responses)

    returned_unit_ids = {res.json()["unit_id"] for res in responses}
    assert expected_unit_ids == returned_unit_ids

    # Verify final database state
    stmt = (
        select(DropUnit)
        .where(DropUnit.drop_id == drop.id)
        .execution_options(populate_existing=True)
    )
    db_units = (await committed_session.scalars(stmt)).all()

    expected_user_ids = {payload["user_id"] for payload in json_payloads}

    assert len(db_units) == unit_count
    assert all(u.status == DropUnitStatus.PURCHASED for u in db_units)
    assert {str(u.held_by_user_id) for u in db_units} == expected_user_ids
