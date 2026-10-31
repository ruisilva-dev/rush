import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import drops as drops_module
from app.models.drop import Drop, DropStatus
from app.models.drop_unit import DropUnit, DropUnitStatus


@pytest.mark.asyncio
async def test_purchase_optimistic_success_uncontended(
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
        f"/drops/{drop.id}/purchase-optimistic",
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
    assert unit.purchased_by_user_id == buyer_id
    assert unit.purchased_at is not None
    assert unit.held_by_user_id is None


@pytest.mark.asyncio
async def test_purchase_optimistic_not_yet_open_future_schedule(
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
        f"/drops/{drop.id}/purchase-optimistic",
        json={"user_id": str(uuid4())},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "not_yet_open"


@pytest.mark.asyncio
async def test_purchase_optimistic_not_yet_open_draft(
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
        f"/drops/{drop.id}/purchase-optimistic",
        json={"user_id": str(uuid4())},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "not_yet_open"


@pytest.mark.asyncio
async def test_purchase_optimistic_sold_out(
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
        purchased_by_user_id=uuid4(),
    )
    db_session.add(unit)
    await db_session.flush()

    response = await client.post(
        f"/drops/{drop.id}/purchase-optimistic",
        json={"user_id": str(uuid4())},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "sold_out"


@pytest.mark.asyncio
async def test_purchase_optimistic_route_errors(client: AsyncClient) -> None:
    res_404 = await client.post(
        f"/drops/{uuid4()}/purchase-optimistic",
        json={"user_id": str(uuid4())},
    )
    assert res_404.status_code == 404

    res_malformed_url = await client.post(
        "/drops/not-a-valid-uuid/purchase-optimistic",
        json={"user_id": str(uuid4())},
    )
    assert res_malformed_url.status_code == 422

    res_malformed_body = await client.post(
        f"/drops/{uuid4()}/purchase-optimistic",
        json={"user_id": "invalid-user-uuid"},
    )
    assert res_malformed_body.status_code == 422

    res_empty_body = await client.post(
        f"/drops/{uuid4()}/purchase-optimistic",
        json={},
    )
    assert res_empty_body.status_code == 422


@pytest.mark.asyncio
async def test_purchase_optimistic_solve_silent_oversell(
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
    url = f"/drops/{drop.id}/purchase-optimistic"

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
    assert str(unit.purchased_by_user_id) in {user_a, user_b}
    assert successes[0].json()["user_id"] == str(unit.purchased_by_user_id)


async def _seed_purchased_and_available(
    session: AsyncSession,
) -> tuple[Drop, DropUnit, DropUnit]:
    drop = Drop(
        name="Contention Drop",
        status=DropStatus.ACTIVE,
        total_units=2,
        scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    session.add(drop)
    await session.flush()

    purchased = DropUnit(
        drop_id=drop.id,
        status=DropUnitStatus.PURCHASED,
        purchased_by_user_id=uuid4(),
    )
    available = DropUnit(drop_id=drop.id, status=DropUnitStatus.AVAILABLE)
    session.add_all([purchased, available])
    await session.flush()
    return drop, purchased, available


@pytest.mark.asyncio
async def test_purchase_optimistic_contention_exhaustion(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drop, purchased, available = await _seed_purchased_and_available(db_session)
    calls: list[UUID] = []

    async def fake(db: AsyncSession, drop_id: UUID) -> UUID | None:
        calls.append(drop_id)
        return purchased.id

    monkeypatch.setattr("app.api.drops._select_claim_candidate", fake)

    res = await client.post(
        f"/drops/{drop.id}/purchase-optimistic", json={"user_id": str(uuid4())}
    )

    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "contention"
    assert len(calls) == drops_module.MAX_CLAIM_ATTEMPTS + 1 == 6

    await db_session.refresh(available)
    assert available.status == DropUnitStatus.AVAILABLE
    assert available.purchased_by_user_id is None


@pytest.mark.asyncio
async def test_purchase_optimistic_final_lost_claim_reports_sold_out(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drop, purchased, _ = await _seed_purchased_and_available(db_session)
    calls: list[UUID] = []

    async def fake(db: AsyncSession, drop_id: UUID) -> UUID | None:
        calls.append(drop_id)
        return purchased.id if len(calls) <= 5 else None

    monkeypatch.setattr("app.api.drops._select_claim_candidate", fake)

    res = await client.post(
        f"/drops/{drop.id}/purchase-optimistic", json={"user_id": str(uuid4())}
    )

    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "sold_out"
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_purchase_optimistic_blocked_claim_retries_next_unit(
    committed_session: AsyncSession,
    concurrent_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drop = Drop(
        name="Blocked Claim Drop",
        status=DropStatus.ACTIVE,
        total_units=2,
        scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    committed_session.add(drop)
    await committed_session.flush()
    u1 = DropUnit(drop_id=drop.id, status=DropUnitStatus.AVAILABLE)
    u2 = DropUnit(drop_id=drop.id, status=DropUnitStatus.AVAILABLE)
    committed_session.add_all([u1, u2])
    await committed_session.commit()
    drop_id, u1_id, u2_id = drop.id, u1.id, u2.id

    real_select = drops_module._select_claim_candidate
    call_count = 0

    async def fake(db: AsyncSession, did: UUID) -> UUID | None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return u1_id
        return await real_select(db, did)

    monkeypatch.setattr("app.api.drops._select_claim_candidate", fake)

    other_buyer = uuid4()
    buyer = uuid4()
    task: asyncio.Task | None = None
    try:
        # Uncommitted claim of U1 by another buyer holds its row lock.
        await committed_session.execute(
            text(
                "UPDATE drop_units SET status='purchased', "
                "purchased_by_user_id=:uid, purchased_at=now() WHERE id=:id"
            ),
            {"uid": other_buyer, "id": u1_id},
        )

        task = asyncio.create_task(
            concurrent_client.post(
                f"/drops/{drop_id}/purchase-optimistic",
                json={"user_id": str(buyer)},
            )
        )

        waiting = 0
        for _ in range(50):
            waiting = await committed_session.scalar(
                text("SELECT count(*) FROM pg_locks WHERE NOT granted")
            )
            if waiting:
                break
            await asyncio.sleep(0.1)
        assert waiting, "request never blocked on the claimed row"
        assert not task.done()

        await committed_session.commit()
        res = await asyncio.wait_for(task, timeout=5)
    finally:
        await committed_session.rollback()
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert res.status_code == 200
    assert res.json()["unit_id"] == str(u2_id)

    stmt = (
        select(DropUnit)
        .where(DropUnit.id == u1_id)
        .execution_options(populate_existing=True)
    )
    first = await committed_session.scalar(stmt)
    assert first is not None
    assert first.purchased_by_user_id == other_buyer


@pytest.mark.asyncio
async def test_purchase_optimistic_multi_unit_contention(
    committed_session: AsyncSession, concurrent_client: AsyncClient
) -> None:
    unit_count = 5
    units = []

    drop = Drop(
        name="Multi Unit Drop",
        status=DropStatus.ACTIVE,
        total_units=unit_count,
        scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    committed_session.add(drop)
    await committed_session.flush()

    for _ in range(unit_count):
        unit = DropUnit(drop_id=drop.id, status=DropUnitStatus.AVAILABLE)
        committed_session.add(unit)
        units.append(unit)
    await committed_session.commit()

    expected_unit_ids = {str(u.id) for u in units}
    payloads = [{"user_id": str(uuid4())} for _ in range(unit_count)]
    url = f"/drops/{drop.id}/purchase-optimistic"

    responses = await asyncio.gather(
        *(concurrent_client.post(url, json=p) for p in payloads)
    )

    assert all(r.status_code == 200 for r in responses)
    assert {r.json()["unit_id"] for r in responses} == expected_unit_ids

    stmt = (
        select(DropUnit)
        .where(DropUnit.drop_id == drop.id)
        .execution_options(populate_existing=True)
    )
    db_units = (await committed_session.scalars(stmt)).all()
    assert len(db_units) == unit_count
    assert all(u.status == DropUnitStatus.PURCHASED for u in db_units)
    assert {str(u.purchased_by_user_id) for u in db_units} == {
        p["user_id"] for p in payloads
    }


@pytest.mark.asyncio
async def test_purchase_optimistic_more_buyers_than_units(
    committed_session: AsyncSession, concurrent_client: AsyncClient
) -> None:
    unit_count, buyer_count = 3, 10

    drop = Drop(
        name="Oversubscribed Drop",
        status=DropStatus.ACTIVE,
        total_units=unit_count,
        scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    committed_session.add(drop)
    await committed_session.flush()
    committed_session.add_all(
        DropUnit(drop_id=drop.id, status=DropUnitStatus.AVAILABLE)
        for _ in range(unit_count)
    )
    await committed_session.commit()
    url = f"/drops/{drop.id}/purchase-optimistic"

    responses = await asyncio.gather(
        *(
            concurrent_client.post(url, json={"user_id": str(uuid4())})
            for _ in range(buyer_count)
        )
    )

    successes = [r for r in responses if r.status_code == 200]
    conflicts = [r for r in responses if r.status_code == 409]
    assert len(successes) == unit_count
    assert len(conflicts) == buyer_count - unit_count
    assert len({r.json()["unit_id"] for r in successes}) == unit_count
    assert all(r.json()["detail"]["code"] == "sold_out" for r in conflicts)

    status = await committed_session.scalar(
        select(Drop.status)
        .where(Drop.id == drop.id)
        .execution_options(populate_existing=True)
    )
    assert status == DropStatus.COMPLETED
