from fastapi import APIRouter, status, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import insert, literal, select, func, Select
from uuid import UUID
from app.schemas.drop import DropResponse, DropCreateRequest
from app.schemas.purchase import PurchaseResponse, PurchaseRequest
from app.core.db import get_db
from app.models.drop import DropStatus, Drop
from app.models.drop_unit import DropUnit, DropUnitStatus


router = APIRouter(prefix="/drops", tags=["Drops"])


def _drop_with_available_count_query() -> Select:
    """Correlated scalar subquery to count available units without full GROUP BY."""
    available_subq = (
        select(func.count(DropUnit.id))
        .where(
            DropUnit.drop_id == Drop.id,
            DropUnit.status == DropUnitStatus.AVAILABLE,
        )
        .scalar_subquery()
    )
    return select(Drop, available_subq.label("available_units"))


@router.post("", response_model=DropResponse, status_code=status.HTTP_201_CREATED)
async def create_drop(
    payload: DropCreateRequest, db: AsyncSession = Depends(get_db)
) -> DropResponse:
    initial_status = (
        DropStatus.SCHEDULED if payload.scheduled_at is not None else DropStatus.DRAFT
    )

    async with db.begin():
        drop = Drop(
            name=payload.name,
            status=initial_status,
            total_units=payload.total_units,
            scheduled_at=payload.scheduled_at,
        )
        db.add(drop)

        # Flush to populate drop.id via server-default uuidv7()
        await db.flush()

        # Bulk DropUnit generation
        # Postgres automatically executes uuidv7() server_default per generated row
        insert_units_stmt = insert(DropUnit).from_select(
            ["drop_id", "status"],
            select(
                literal(drop.id).label("drop_id"),
                literal(
                    DropUnitStatus.AVAILABLE.value, type_=DropUnit.status.type
                ).label("status"),
            ).select_from(func.generate_series(1, drop.total_units)),
        )
        await db.execute(insert_units_stmt)

    return DropResponse(
        id=drop.id,
        name=drop.name,
        status=drop.status,
        total_units=drop.total_units,
        available_units=drop.total_units,
        scheduled_at=drop.scheduled_at,
        created_at=drop.created_at,
        updated_at=drop.updated_at,
    )


@router.get("", response_model=list[DropResponse])
async def list_drops(db: AsyncSession = Depends(get_db)) -> list[DropResponse]:
    query = _drop_with_available_count_query().order_by(Drop.created_at.desc())
    result = await db.execute(query)

    return [
        DropResponse(
            id=drop.id,
            name=drop.name,
            status=drop.status,
            total_units=drop.total_units,
            available_units=available_count,
            scheduled_at=drop.scheduled_at,
            created_at=drop.created_at,
            updated_at=drop.updated_at,
        )
        for drop, available_count in result.all()
    ]


@router.get("/{id}", response_model=DropResponse)
async def get_drop(id: UUID, db: AsyncSession = Depends(get_db)) -> DropResponse:
    query = _drop_with_available_count_query().where(Drop.id == id)
    result = await db.execute(query)
    row = result.first()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Drop with ID '{id}' not found",
        )

    drop, available_count = row
    return DropResponse(
        id=drop.id,
        name=drop.name,
        status=drop.status,
        total_units=drop.total_units,
        available_units=available_count,
        scheduled_at=drop.scheduled_at,
        created_at=drop.created_at,
        updated_at=drop.updated_at,
    )


@router.post("/{id}/purchase-naive", response_model=PurchaseResponse)
async def purchase_naive(
    id: UUID, payload: PurchaseRequest, db: AsyncSession = Depends(get_db)
) -> PurchaseResponse:
    async with db.begin():
        stmt = select(Drop, func.now()).where(Drop.id == id)
        result = await db.execute(stmt)
        row = result.first()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Drop with ID '{id}' not found",
            )

        drop, db_now = row
        if drop.scheduled_at is None or db_now <= drop.scheduled_at:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "not_yet_open", "message": "Drop isn't open yet."},
            )

        stmt = (
            select(DropUnit)
            .where(DropUnit.drop_id == id, DropUnit.status == DropUnitStatus.AVAILABLE)
            .limit(1)
        )

        unit = await db.scalar(stmt)
        if not unit:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "sold_out",
                    "message": "No units remaining for this drop.",
                },
            )

        # VULNERABLE: Baseline implementation without row-level locking
        # (e.g., SELECT ... FOR UPDATE).
        # Under concurrent load, multiple transactions can read and claim the same unit,
        # causing overselling.
        unit.status = DropUnitStatus.PURCHASED
        unit.held_by_user_id = payload.user_id

        # Validate inside the transaction: a failure rolls back the purchase.
        response = PurchaseResponse.model_validate(unit)

    return response


@router.post("/{id}/purchase-pessimistic", response_model=PurchaseResponse)
async def purchase_pessimistic(
    id: UUID, payload: PurchaseRequest, db: AsyncSession = Depends(get_db)
) -> PurchaseResponse:
    async with db.begin():
        stmt = select(Drop, func.now()).where(Drop.id == id)
        result = await db.execute(stmt)
        row = result.first()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Drop with ID '{id}' not found",
            )

        drop, db_now = row
        if drop.scheduled_at is None or db_now <= drop.scheduled_at:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "not_yet_open", "message": "Drop isn't open yet."},
            )

        stmt = (
            select(DropUnit)
            .where(DropUnit.drop_id == id, DropUnit.status == DropUnitStatus.AVAILABLE)
            .with_for_update(skip_locked=True)
            .limit(1)
        )

        unit = await db.scalar(stmt)

        # SKIP LOCKED trade-off:
        # An empty result means no *unlocked* available units exist right now.
        # If a concurrent transaction holding a unit later rolls back, a client
        # may have received sold_out even though inventory briefly returned.
        if not unit:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "sold_out",
                    "message": "No units currently available for this drop.",
                },
            )

        unit.status = DropUnitStatus.PURCHASED
        unit.held_by_user_id = payload.user_id

        # Validate inside the transaction: a failure rolls back the purchase.
        response = PurchaseResponse.model_validate(unit)

    return response
