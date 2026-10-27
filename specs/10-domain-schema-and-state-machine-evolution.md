# Technical Specification: Domain Schema & State Machine Evolution (Group 4)

## 1. Overview, Scope and Requirements

### Problem Statement
The domain models in [app/models/drop.py](file:///home/rilay/rush/app/models/drop.py) and [app/models/drop_unit.py](file:///home/rilay/rush/app/models/drop_unit.py) contain two architectural design compromises:
1. **Semantic Field Conflation & Incomplete Hold Architecture** ([app/models/drop_unit.py#L43-L46](file:///home/rilay/rush/app/models/drop_unit.py#L43-L46), [app/schemas/purchase.py#L13](file:///home/rilay/rush/app/schemas/purchase.py#L13)):
   * `DropUnit` defines `held_by_user_id`, `hold_expires_at`, and status `HELD`. However, no hold reservation endpoints or expiration workers exist.
   * Finalized purchases store the buyer's UUID inside `held_by_user_id` alongside `status = DropUnitStatus.PURCHASED`.
   * `PurchaseResponse` aliases `user_id` to `held_by_user_id`. This conflates temporary reservations with final purchases, preventing a real hold/cart reservation system from being built without breaking changes.
2. **Static Drop Lifecycle State Machine** ([app/models/drop.py#L14-L18](file:///home/rilay/rush/app/models/drop.py#L14-L18)):
   * `DropStatus` defines four states: `DRAFT`, `SCHEDULED`, `ACTIVE`, and `COMPLETED`.
   * Drops are only ever set to `SCHEDULED` (with `scheduled_at`) or `DRAFT` (without `scheduled_at`) upon creation.
   * The API never transitions a drop to `ACTIVE` once `scheduled_at` has passed, nor to `COMPLETED` when inventory is fully exhausted. Drops remain permanently stuck in `SCHEDULED` or `DRAFT`.

### Explicit Non-Goals
* Implementing a complete distributed hold-expiration worker queue (Redis / Celery) in this iteration; the schema will support holds cleanly while decoupling purchases.
* Breaking backwards compatibility with the existing HTTP response contracts.

### Functional Requirements & Acceptance Criteria
* **AC-4.1**: Separate final purchase ownership from temporary holds in `drop_units`:
  * Add `purchased_by_user_id: Mapped[UUID | None]`
  * Add `purchased_at: Mapped[datetime | None]`
  * Keep `held_by_user_id` and `hold_expires_at` exclusively for `DropUnitStatus.HELD`.
* **AC-4.2**: Generate and apply an Alembic migration adding the new columns, creating the partial index, and performing a data migration to backfill `purchased_by_user_id` from `held_by_user_id` for historical purchased units.
* **AC-4.3**: Update `PurchaseResponse` to source `user_id` from `purchased_by_user_id`.
* **AC-4.4**: Implement automated Drop lifecycle state transitions:
  * Transition from `SCHEDULED` to `ACTIVE` when `db_now >= drop.scheduled_at`.
  * Transition to `COMPLETED` via an atomic conditional SQL update when the final available unit is purchased, resilient to concurrent checkout race conditions.
* **AC-4.5**: Update all existing unit and concurrency test assertions in `tests/test_purchase_pessimistic.py` and `tests/test_purchase_naive.py` to assert against `purchased_by_user_id` and `purchased_at`.
* **AC-4.6**: Enforce `native_enum=True` on `Drop.status` and `DropUnit.status` mapping to align with project conventions.

---

## 2. Architecture and Execution Flow

```
Drop Lifecycle State Machine:
┌─────────┐   Add Schedule    ┌───────────┐   db_now >= scheduled_at   ┌──────────┐   Inventory = 0   ┌───────────┐
│  DRAFT  │ ────────────────► │ SCHEDULED │ ─────────────────────────► │  ACTIVE  │ ────────────────► │ COMPLETED │
└─────────┘                   └───────────┘                            └──────────┘                   └───────────┘

DropUnit Lifecycle State Machine:
┌───────────┐   Reserve Hold    ┌──────────┐   Finalize Purchase   ┌───────────┐
│ AVAILABLE │ ────────────────► │   HELD   │ ────────────────────► │ PURCHASED │
└───────────┘                   └──────────┘                       └───────────┘
      │                                                                  ▲
      └────────────────────── Direct Purchase (Current) ─────────────────┘
                               (sets purchased_by_user_id & purchased_at)
```

---

## 3. Interfaces, State and Contracts

### Updated `DropUnit` Model (`app/models/drop_unit.py`)
```python
class DropUnit(Base):
    __tablename__ = "drop_units"
    __table_args__ = (
        Index(
            "ix_drop_units_drop_id_available",
            "drop_id",
            postgresql_where=text("status = 'available'"),
        ),
        Index(
            "ix_drop_units_purchased_by_user_id",
            "purchased_by_user_id",
            postgresql_where=text("purchased_by_user_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    drop_id: Mapped[UUID] = mapped_column(
        ForeignKey("drops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[DropUnitStatus] = mapped_column(
        SAEnum(
            DropUnitStatus,
            name="drop_unit_status",
            native_enum=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        default=DropUnitStatus.AVAILABLE,
        nullable=False,
    )
    # Temporary hold fields
    held_by_user_id: Mapped[UUID | None] = mapped_column(nullable=True)
    hold_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Finalized purchase fields
    purchased_by_user_id: Mapped[UUID | None] = mapped_column(nullable=True)
    purchased_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    drop: Mapped["Drop"] = relationship(back_populates="units")
```

### Updated `Drop` Model Enum Mapping (`app/models/drop.py`)
```python
    status: Mapped[DropStatus] = mapped_column(
        SAEnum(
            DropStatus,
            name="drop_status",
            native_enum=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        default=DropStatus.DRAFT,
        nullable=False,
    )
```

### Updated Purchase Response Schema (`app/schemas/purchase.py`)
```python
class PurchaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    user_id: UUID = Field(validation_alias="purchased_by_user_id")
    drop_id: UUID
    unit_id: UUID = Field(validation_alias="id")
    status: DropUnitStatus
```

### Atomic State Machine Transitions (`app/api/drops.py`)
```python
# Transition drop to ACTIVE if schedule passed and currently SCHEDULED
if drop.status == DropStatus.SCHEDULED and db_now >= drop.scheduled_at:
    drop.status = DropStatus.ACTIVE

unit.status = DropUnitStatus.PURCHASED
unit.purchased_by_user_id = payload.user_id
unit.purchased_at = db_now
await db.flush()

# Atomic conditional update transitions drop to COMPLETED only when no available units remain.
# Under PostgreSQL READ COMMITTED, this query re-evaluates ~has_available against committed rows,
# preventing the race condition where concurrent checkouts of the last units strand drops in ACTIVE.
has_available = (
    select(DropUnit.id)
    .where(
        DropUnit.drop_id == drop.id,
        DropUnit.status == DropUnitStatus.AVAILABLE,
    )
    .exists()
)
await db.execute(
    update(Drop)
    .where(Drop.id == drop.id, ~has_available)
    .values(status=DropStatus.COMPLETED)
)
```

### Alembic Migration Specification (`alembic/versions/...`)
```python
def upgrade() -> None:
    op.add_column(
        "drop_units", sa.Column("purchased_by_user_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "drop_units",
        sa.Column("purchased_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_drop_units_purchased_by_user_id",
        "drop_units",
        ["purchased_by_user_id"],
        unique=False,
        postgresql_where=sa.text("purchased_by_user_id IS NOT NULL"),
    )
    # Backfill historical purchased records
    op.execute(
        """
        UPDATE drop_units
        SET purchased_by_user_id = held_by_user_id,
            held_by_user_id = NULL
        WHERE status = 'purchased';
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_drop_units_purchased_by_user_id",
        table_name="drop_units",
        postgresql_where=sa.text("purchased_by_user_id IS NOT NULL"),
    )
    op.drop_column("drop_units", "purchased_at")
    op.drop_column("drop_units", "purchased_by_user_id")
```

---

## 4. Security and Trust Boundaries
* **Audit Trail**: Tracking `purchased_at` and `purchased_by_user_id` permanently decouples buyer identity from temporary reservation state, providing immutable purchase records.
* **Partial Index Isolation**: Partial index `ix_drop_units_purchased_by_user_id` indexes only non-null values, preventing index bloat while speeding up user purchase history queries.

---

## 5. Performance, Scalability and Resource Budgets
* **Exhaustion Check Efficiency**: The `~has_available` subquery leverages the partial index `ix_drop_units_drop_id_available`. If units remain, `EXISTS` terminates on the first index match (<0.2ms).
* **Minimal Lock Footprint**: The `update(Drop)` query executes in milliseconds at the end of the checkout transaction, acquiring a row lock on `Drop` only when completing the drop.

---

## 6. Failure Modes, Edge Cases and Observability
* **Concurrent Last-Unit Checkouts**: If two transactions purchase the final 2 units simultaneously:
  1. Both transactions claim distinct `DropUnit` rows and flush.
  2. The first transaction to reach `update(Drop)` finds units still available and updates 0 rows.
  3. The second transaction reaches `update(Drop)`, waits for the first to commit, re-evaluates `~has_available`, detects 0 available units, and transitions the drop to `COMPLETED`.
* **Migration Rollback**: Downgrade path drops `purchased_by_user_id` and `purchased_at` without affecting base table integrity.

---

## 7. Architecture Decisions and Trade-offs (ADR Log)

### ADR 4.1: Dedicated Purchase Columns vs Overloading Hold Fields
* **Context**: `held_by_user_id` was previously overloaded to store buyer IDs on purchased units.
* **Decision**: Add `purchased_by_user_id` and `purchased_at`. Reserve `held_by_user_id` and `hold_expires_at` exclusively for `HELD` status.
* **Trade-off**: Requires an Alembic migration, but eliminates semantic debt and unblocks future cart/hold feature development.

### ADR 4.2: Atomic Conditional UPDATE vs Non-Locking `SELECT count(*)`
* **Context**: Under PostgreSQL MVCC (`READ COMMITTED`), concurrent purchases of the last units do not see uncommitted purchases from peer transactions when using `SELECT count(*)`, causing drops to remain stuck in `ACTIVE`.
* **Decision**: Use `update(Drop).where(Drop.id == drop.id, ~has_available).values(status=DropStatus.COMPLETED)`.
* **Alternatives Rejected**: `select(func.count(DropUnit.id))` (vulnerable to race conditions).
* **Trade-off**: Acquires a brief row-level lock on `Drop` at the end of the transaction, but guarantees absolute state consistency under high concurrency.
