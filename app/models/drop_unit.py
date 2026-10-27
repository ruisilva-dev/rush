from uuid import UUID
from sqlalchemy import text, ForeignKey, DateTime, func, Index
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from enum import StrEnum
from datetime import datetime
from app.models.base import Base
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.drop import Drop


class DropUnitStatus(StrEnum):
    AVAILABLE = "available"
    HELD = "held"
    PURCHASED = "purchased"


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
