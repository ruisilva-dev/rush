from datetime import datetime, timezone
from pydantic import BaseModel, Field, field_validator, ConfigDict
from uuid import UUID
from app.models.drop import DropStatus


class DropCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    total_units: int = Field(ge=1, le=10000)
    scheduled_at: datetime | None = None

    @field_validator("scheduled_at")
    @classmethod
    def validate_scheduled_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None

        # Check for timezone awareness - tzinfo and UTC offset
        if value.utcoffset() is None:
            raise ValueError("scheduled_at must be timezone-aware (e.g. UTC)")

        now_utc = datetime.now(timezone.utc)
        if value <= now_utc:
            raise ValueError("scheduled_at must be set in the future")

        return value


class DropResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    status: DropStatus
    total_units: int
    available_units: int
    scheduled_at: datetime | None
    created_at: datetime
    updated_at: datetime
