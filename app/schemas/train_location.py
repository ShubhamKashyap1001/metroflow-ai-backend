from pydantic import BaseModel
from pydantic import ConfigDict


class TrainLocationCreate(BaseModel):
    train_id: int
    station_id: int


class TrainLocationResponse(BaseModel):
    id: int
    train_id: int
    station_id: int
    model_config = ConfigDict(
        from_attributes=True
    )