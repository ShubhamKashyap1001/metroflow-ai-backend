from pydantic import BaseModel
from pydantic import ConfigDict

from app.enums.train_status import TrainStatus


class TrainCreate(BaseModel):
    train_number: str
    capacity: int


class TrainUpdate(BaseModel):
    capacity: int | None = None
    status: TrainStatus | None = None
    is_active: bool | None = None


class TrainResponse(BaseModel):
    id: int
    train_number: str
    capacity: int
    status: TrainStatus
    is_active: bool
    model_config = ConfigDict(
        from_attributes=True
    )