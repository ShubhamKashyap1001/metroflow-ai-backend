from datetime import datetime

from pydantic import BaseModel
from pydantic import ConfigDict

from app.enums.journey_status import JourneyStatus


class JourneyCreate(BaseModel):
    user_id: str
    source_station_id: int
    destination_station_id: int
    checkin_time: datetime


class JourneyUpdate(BaseModel):
    checkout_time: datetime | None = None
    fare: float | None = None
    status: JourneyStatus | None = None


class JourneyResponse(BaseModel):
    id: int
    user_id: str
    source_station_id: int
    destination_station_id: int
    checkin_time: datetime
    checkout_time: datetime | None
    fare: float
    status: JourneyStatus
    model_config = ConfigDict(
        from_attributes=True
    )