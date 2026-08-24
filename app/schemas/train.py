from pydantic import BaseModel
from pydantic import ConfigDict
from app.enums.train_status import TrainStatus

class TrainLiveResponse(BaseModel):
    train_id: int
    train_number: str
    from_station_id: int
    from_station_name: str | None = None
    to_station_id: int
    to_station_name: str | None = None
    progress_ratio: float
    delay_minutes: int
    status: str
    eta_seconds: int | None = None
    segment_duration_seconds: int | None = None
    direction: int = 1

class TrainRouteResponse(BaseModel):
    train_id: int
    train_number: str
    station_ids: list[int]
    station_names: list[str | None]
    segment_seconds: list[int]

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