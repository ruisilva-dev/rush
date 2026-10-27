# Technical Specification: API Transaction & Serialization Contracts (Group 3)

## 1. Overview, Scope and Requirements

### Problem Statement
The HTTP endpoints in [app/api/drops.py](file:///home/rilay/rush/app/api/drops.py) have two major architectural inconsistencies:
1. **Divergent Transaction Boundary Ownership** ([app/api/drops.py#L36](file:///home/rilay/rush/app/api/drops.py#L36), [app/api/drops.py#L159](file:///home/rilay/rush/app/api/drops.py#L159), [app/api/drops.py#L210](file:///home/rilay/rush/app/api/drops.py#L210)):
   * `create_drop` defines an explicit transaction block with `async with db.begin():`.
   * `purchase_naive` and `purchase_pessimistic` rely on implicit SQLAlchemy autobegin on their first `select()`, followed by an explicit `await db.commit()`.
   * Read endpoints (`list_drops`, `get_drop`) execute queries directly via `await db.execute()` without write transactions.
   * Mixing `async with db.begin():` with autobegun queries triggers `InvalidRequestError: A transaction is already begun on this Session`.
2. **Inconsistent Response Serialization & Post-Commit Validation Hazard** ([app/api/drops.py#L61](file:///home/rilay/rush/app/api/drops.py#L61), [app/api/drops.py#L161](file:///home/rilay/rush/app/api/drops.py#L161)):
   * `create_drop`, `list_drops`, and `get_drop` return validated Pydantic `DropResponse` instances.
   * `purchase_naive` and `purchase_pessimistic` return the raw SQLAlchemy ORM model directly (`return unit`), relying on `response_model=PurchaseResponse` and `expire_on_commit=False`.
   * Performing Pydantic validation *after* transaction commit introduces an orphaned commit hazard: if validation fails, the unit is already committed as purchased in PostgreSQL, while the client receives a 500 Internal Server Error.

### Explicit Non-Goals
* Altering the database schema or introducing new columns (belongs in Group 4).
* Changing public API contracts or HTTP status codes (200, 201, 404, 409, 422).

### Functional Requirements & Acceptance Criteria
* **AC-3.1**: All mutating (write) endpoints (`create_drop`, `purchase_naive`, `purchase_pessimistic`) must encapsulate all database interactions inside an atomic `async with db.begin():` block.
* **AC-3.2**: Manual calls to `await db.commit()` in route handlers must be eliminated in favor of automatic block-level commits upon clean exit.
* **AC-3.3**: Any `HTTPException` or serialization validation error raised inside a write route must automatically roll back the transaction upon exiting the `async with db.begin():` block.
* **AC-3.4**: All endpoints must explicitly return validated Pydantic model instances. Model validation (`PurchaseResponse.model_validate(unit)`) must occur **inside** the transaction boundary before commit.
* **AC-3.5**: Read-only endpoints (`list_drops`, `get_drop`) execute queries directly via `await db.execute(query)` without explicit `db.begin()` blocks, relying on `get_db()` to safely close the session on exit.

---

## 2. Architecture and Execution Flow

```
Incoming HTTP Request
        │
        ▼
Dependency Injection: db = Depends(get_db)
        │
        ├─► [Read Endpoint (GET)]:
        │     1. result = await db.execute(query)
        │     2. return [ResponseModel(...) for ...]
        │
        └─► [Write Endpoint (POST)]:
              async with db.begin():
                  1. Check constraints & execute queries
                  2. If violation: raise HTTPException (Triggers Auto-Rollback)
                  3. Mutate ORM entities
                  4. response = ResponseModel.model_validate(entity) [Inside Tx]
                  5. [Clean Exit Triggers Auto-Commit]
              return response
```

---

## 3. Interfaces, State and Contracts

### Canonical Pessimistic Purchase Endpoint (`app/api/drops.py`)
```python
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

        unit_stmt = (
            select(DropUnit)
            .where(DropUnit.drop_id == id, DropUnit.status == DropUnitStatus.AVAILABLE)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        unit = await db.scalar(unit_stmt)

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

        # Validate INSIDE transaction block: if validation fails, transaction rolls back
        response = PurchaseResponse.model_validate(unit)

    return response
```

### Canonical Naive Purchase Endpoint (`app/api/drops.py`)
```python
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

        # Baseline naive query without row-level locking (demonstrates oversell race condition)
        unit_stmt = (
            select(DropUnit)
            .where(DropUnit.drop_id == id, DropUnit.status == DropUnitStatus.AVAILABLE)
            .limit(1)
        )
        unit = await db.scalar(unit_stmt)

        if not unit:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "sold_out",
                    "message": "No units remaining for this drop.",
                },
            )

        unit.status = DropUnitStatus.PURCHASED
        unit.held_by_user_id = payload.user_id

        # Validate INSIDE transaction block
        response = PurchaseResponse.model_validate(unit)

    return response
```

### Canonical Read-Only Pattern (`app/api/drops.py`)
```python
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
```

---

## 4. Security and Trust Boundaries
* **Atomic Failure Containment**: If `sold_out`, `not_yet_open`, or a Pydantic serialization error occurs, exiting `async with db.begin():` automatically releases all acquired locks and rolls back pending mutations.
* **Orphaned Purchase Prevention**: Validating the response model inside the transaction guarantees that a client receiving a 500 serialization error never leaves an orphaned purchased row behind.

---

## 5. Performance, Scalability and Resource Budgets
* **Lock Duration**: In `purchase_pessimistic`, the lock on `DropUnit` is acquired and committed within the compact `async with db.begin():` block, holding the row lock for <10ms.
* **No Redundant Queries**: Eliminating manual `commit()` prevents unnecessary round-trips and eliminates session expiration refetches.

---

## 6. Failure Modes, Edge Cases and Observability
* **Client Disconnect**: Handled cleanly by `get_db()` catching `BaseException` (Group 1) and `async with db.begin():` issuing a rollback on unhandled exit.
* **Validation Error Inside Transaction**: If `PurchaseResponse.model_validate(unit)` fails, an exception is raised before `db.begin()` exits, causing an immediate database rollback.

---

## 7. Architecture Decisions and Trade-offs (ADR Log)

### ADR 3.1: Explicit `async with db.begin():` vs Implicit Autobegin + `await db.commit()`
* **Context**: Routes previously mixed `db.begin()` with manual `db.commit()`.
* **Decision**: All write endpoints standardize on `async with db.begin():`.
* **Alternatives Rejected**: Relying on autobegin with manual `commit()` (leaves error handling vulnerable to unclosed transactions when exceptions occur before commit).
* **Trade-off**: Requires all queries for a write operation to reside inside the context block.

### ADR 3.2: Model Validation Inside vs Outside Transaction Boundary
* **Context**: Executing `model_validate()` after `db.begin():` exits leaves the database committed if serialization fails.
* **Decision**: Execute `model_validate()` inside `async with db.begin():`.
* **Trade-off**: Retains the database transaction open for an extra ~0.1ms of in-memory CPU serialization in exchange for ironclad consistency guarantees.

### ADR 3.3: Read-Only Query Transactions
* **Context**: Should read endpoints (`GET /drops`, `GET /drops/{id}`) be wrapped in `async with db.begin():`?
* **Decision**: No. Read endpoints run directly via `await db.execute(query)`.
* **Rationale**: Wrapping reads in write transaction blocks adds unnecessary transaction overhead and lock accounting in PostgreSQL. `get_db()` automatically closes and rolls back the session on generator exit.
