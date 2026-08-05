from pydantic import BaseModel
from pydantic import ConfigDict


class StationCreate(BaseModel):
    station_code: str
    station_name: str
    city: str
    latitude: float
    longitude: float
    is_interchange: bool = False


class StationUpdate(BaseModel):
    station_name: str | None = None
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    is_interchange: bool | None = None
    is_active: bool | None = None


class StationResponse(BaseModel):
    id: int
    station_code: str
    station_name: str
    city: str
    latitude: float
    longitude: float
    is_interchange: bool
    is_active: bool
    model_config = ConfigDict(
        from_attributes=True
    )