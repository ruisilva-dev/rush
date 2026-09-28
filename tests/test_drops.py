import pytest
from unittest.mock import patch
from uuid import uuid4
from datetime import datetime, timezone, timedelta
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, Result
from app.models.drop_unit import DropUnit
from app.models.drop import DropStatus, Drop


@pytest.mark.asyncio
async def test_create_drop_success(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    scheduled_time = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    payload = {
        "name": "Limited Edition Sneakers",
        "total_units": 50,
        "scheduled_at": scheduled_time,
    }

    response = await client.post("/drops", json=payload)

    # Verify HTTP response
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == payload["name"]
    assert data["total_units"] == 50
    assert data["available_units"] == 50
    assert data["status"] == DropStatus.SCHEDULED
    assert "id" in data

    unit_count = await db_session.scalar(
        select(func.count(DropUnit.id)).where(DropUnit.drop_id == data["id"])
    )
    assert unit_count == 50


@pytest.mark.asyncio
async def test_create_drop_draft_without_schedule(client: AsyncClient) -> None:
    payload = {"name": "Draft Drop", "total_units": 10}
    response = await client.post("/drops", json=payload)

    assert response.status_code == 201
    assert response.json()["status"] == DropStatus.DRAFT


@pytest.mark.asyncio
async def test_create_drop_validation_errors(client: AsyncClient) -> None:
    # total_units bounds (min 1, max 10000)
    res_zero = await client.post("/drops", json={"name": "Test", "total_units": 0})
    assert res_zero.status_code == 422

    res_over = await client.post("/drops", json={"name": "Test", "total_units": 10001})
    assert res_over.status_code == 422

    # Missing fields
    res_name = await client.post("/drops", json={"total_units": 1})
    assert res_name.status_code == 422

    res_units = await client.post("/drops", json={"name": "Test"})
    assert res_units.status_code == 422

    res_empty = await client.post("/drops", json={})
    assert res_empty.status_code == 422

    # scheduled_at in the past
    past_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    res_past = await client.post(
        "/drops", json={"name": "Test", "total_units": 10, "scheduled_at": past_time}
    )
    assert res_past.status_code == 422

    # Naive timezone rejection
    res_naive = await client.post(
        "/drops",
        json={"name": "Test", "total_units": 10, "scheduled_at": "2027-01-01T12:00:00"},
    )
    assert res_naive.status_code == 422


@pytest.mark.asyncio
async def test_create_drop_atomic_rollback_on_unit_generation_failure(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    payload = {"name": "Failing Drop", "total_units": 25}

    # Simulate a mid-transaction crash during unit insertion
    original_execute = db_session.execute

    async def mock_execute(stmt: object, *args: object, **kwargs: object) -> Result:
        if str(stmt).strip().startswith("INSERT INTO drop_units"):
            raise RuntimeError("Database connection dropped during unit provisioning")
        return await original_execute(stmt, *args, **kwargs)

    with patch.object(db_session, "execute", side_effect=mock_execute):
        try:
            await client.post("/drops", json=payload)
        except Exception:
            pass

    # Confirm atomic rollback: no orphan Drop was committed
    orphan_drop = await db_session.scalar(
        select(Drop).where(Drop.name == "Failing Drop")
    )
    assert orphan_drop is None


@pytest.mark.asyncio
async def test_get_drop_by_id_and_listing(client: AsyncClient) -> None:
    create_res = await client.post(
        "/drops", json={"name": "Query Drop", "total_units": 5}
    )
    drop_id = create_res.json()["id"]

    # GET /drops/{id} found
    res = await client.get(f"/drops/{drop_id}")
    assert res.status_code == 200
    assert res.json()["name"] == "Query Drop"
    assert res.json()["available_units"] == 5

    # GET /drops listing contains the drop
    list_res = await client.get("/drops")
    assert list_res.status_code == 200
    drop_ids = [d["id"] for d in list_res.json()]
    assert drop_id in drop_ids

    # Non-existent UUID -> 404
    res_404 = await client.get(f"/drops/{uuid4()}")
    assert res_404.status_code == 404

    # Malformed string -> 422
    res_422 = await client.get("/drops/not-a-valid-uuid")
    assert res_422.status_code == 422
