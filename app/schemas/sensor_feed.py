from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

class SensorCrowdRecord(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    station_code: str = Field(min_length=1, max_length=20)
    current_count: int = Field(ge=0)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        return value

class SensorCrowdBatch(BaseModel):
    records: list[SensorCrowdRecord] = Field(min_length=1, max_length=50)

class SensorCrowdBatchResult(BaseModel):
    accepted: int
    duplicates: int
    rejected: int = 0
    model_config = ConfigDict(extra="forbid")

class TrainPositionRecord(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    train_number: str = Field(min_length=1, max_length=30)
    station_code: str = Field(min_length=1, max_length=20)
    next_station_code: str | None = Field(default=None, max_length=20)
    progress_ratio: float = Field(ge=0, le=1)
    status: Literal["in_transit", "at_station", "delayed", "out_of_service"] = "in_transit"
    delay_minutes: int = Field(default=0, ge=0, le=1440)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        return value

class TrainPositionBatch(BaseModel):
    records: list[TrainPositionRecord] = Field(min_length=1, max_length=100)
