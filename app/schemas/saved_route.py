from datetime import time

from pydantic import BaseModel
from pydantic import ConfigDict

from app.enums.crowd_level import CrowdLevel
from app.enums.schedule_status import ScheduleStatus


class SavedRouteCreate(BaseModel):
    origin_station_id: int
    destination_station_id: int


class SavedRouteResponse(BaseModel):
    id: int
    origin_station_id: int
    destination_station_id: int
    origin_station_name: str
    destination_station_name: str
    model_config = ConfigDict(from_attributes=True)


class NextDeparture(BaseModel):
    train_id: int
    platform_number: int
    departure_time: time
    eta_minutes: int
    status: ScheduleStatus
    delay_minutes: int
    matches_destination: bool


class SavedRouteLiveStatus(BaseModel):
    origin_station_id: int
    destination_station_id: int
    origin_station_name: str
    destination_station_name: str
    next_departure: NextDeparture | None = None
    origin_crowd_level: CrowdLevel | None = None
    destination_crowd_level: CrowdLevel | None = None
    message: str | None = None