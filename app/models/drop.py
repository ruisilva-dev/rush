from datetime import datetime
from sqlalchemy import text, String, DateTime, func, CheckConstraint, Integer
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid import UUID
from enum import StrEnum
from app.models.base import Base
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.drop_unit import DropUnit


class DropStatus(StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    COMPLETED = "completed"


class Drop(Base):
    __tablename__ = "drops"
    __table_args__ = (
        CheckConstraint(
            "total_units > 0 AND total_units <= 10000",
            name="ck_drops_total_units_bounds",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("uuidv7()"))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Status transitions (draft -> scheduled -> active -> completed) are not yet
    # driven by anything — no endpoint sets them, and nothing checks `status`
    # before allowing a purchase. Decide in Issue #4 whether transitions are
    # time-computed (derived from scheduled_at) or explicitly set/stored, and
    # whether the purchase endpoint should gate on this field at all.
    status: Mapped[DropStatus] = mapped_column(
        SAEnum(
            DropStatus,
            name="drop_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        default=DropStatus.DRAFT,
        nullable=False,
    )
    total_units: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(
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
    units: Mapped[list["DropUnit"]] = relationship(
        back_populates="drop", cascade="all, delete-orphan"
    )
