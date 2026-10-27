from pydantic import BaseModel, ConfigDict, Field
from uuid import UUID
from app.models.drop_unit import DropUnitStatus


class PurchaseRequest(BaseModel):
    user_id: UUID


class PurchaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    user_id: UUID = Field(validation_alias="purchased_by_user_id")
    drop_id: UUID
    unit_id: UUID = Field(validation_alias="id")
    status: DropUnitStatus
