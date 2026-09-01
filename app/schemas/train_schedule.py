from datetime import time

from pydantic import BaseModel
from pydantic import ConfigDict

from app.enums.day_type import DayType
from app.enums.schedule_status import ScheduleStatus

class TrainScheduleCreate(BaseModel):
    train_id: int
    station_id: int
    arrival_time: time
    departure_time: time
    platform_number: int
    # Position of this station in the train's route (1 = first stop,
    # 2 = second, ...). Optional on create since not every caller
    # knows the full route up front, but strongly recommended: without
    # it, "next stop" resolution in get_upcoming_schedules() can't
    # place this station relative to the train's other stops.
    station_sequence: int | None = None
    day_type: DayType = DayType.WEEKDAY
    is_peak_hour: bool = False
    frequency_minutes: int = 10

class TrainScheduleUpdate(BaseModel):
    arrival_time: time | None = None
    departure_time: time | None = None
    platform_number: int | None = None
    station_sequence: int | None = None
    day_type: DayType | None = None
    is_peak_hour: bool | None = None
    frequency_minutes: int | None = None
    status: ScheduleStatus | None = None

class DelayUpdate(BaseModel):
    """Payload for the delay-handling workflow."""
    delay_minutes: int
    reason: str | None = None

class FrequencyAdjustment(BaseModel):
    """Payload for manually adjusting train frequency for a station/line slot."""
    frequency_minutes: int
    is_peak_hour: bool | None = None

class TrainScheduleResponse(BaseModel):
    id: int
    train_id: int
    station_id: int
    arrival_time: time
    departure_time: time
    platform_number: int
    station_sequence: int | None = None
    day_type: DayType
    is_peak_hour: bool
    frequency_minutes: int
    status: ScheduleStatus
    delay_minutes: int
    actual_arrival_time: time | None = None
    actual_departure_time: time | None = None
    model_config = ConfigDict(
        from_attributes=True
    )
