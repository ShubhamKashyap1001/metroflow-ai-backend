from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

class ReportScheduleCreate(BaseModel):
    frequency: Literal["daily", "weekly"] = "weekly"
    delivery_hour_utc: int = Field(default=8, ge=0, le=23)
    day_of_week: int | None = Field(default=0, ge=0, le=6)
    period_days: int = Field(default=7, ge=1, le=30)
    station_id: int | None = Field(default=None, gt=0)
    line_name: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def validate_frequency(self):
        if self.frequency == "weekly" and self.day_of_week is None:
            raise ValueError("day_of_week is required for weekly reports")
        if self.frequency == "daily":
            self.day_of_week = None
        return self

class ReportScheduleUpdate(BaseModel):
    frequency: Literal["daily", "weekly"] | None = None
    delivery_hour_utc: int | None = Field(default=None, ge=0, le=23)
    day_of_week: int | None = Field(default=None, ge=0, le=6)
    period_days: int | None = Field(default=None, ge=1, le=30)
    station_id: int | None = Field(default=None, gt=0)
    line_name: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=50)
    is_active: bool | None = None

class ReportScheduleResponse(BaseModel):
    id: int
    frequency: str
    delivery_hour_utc: int
    day_of_week: int | None
    period_days: int
    station_id: int | None
    station_name: str | None = None
    line_name: str | None
    state: str | None
    is_active: bool
    next_run_at: datetime
    last_run_at: datetime | None
    last_run_status: str | None
    run_attempts: int
    last_run_error: str | None
    delivery_email: str | None = None

    model_config = {"from_attributes": True}

class DataFreshnessRow(BaseModel):
    station_id: int
    station_name: str
    city: str
    line_name: str | None
    latest_crowd_log_at: datetime | None
    age_seconds: int | None
    status: Literal["fresh", "stale", "missing"]

class DataFreshnessReport(BaseModel):
    observed_at: datetime
    stale_after_seconds: int
    total_stations: int
    fresh_stations: int
    stale_stations: int
    missing_stations: int
    stations: list[DataFreshnessRow]

class TrafficComparisonStation(BaseModel):
    station_id: int
    station_name: str
    current_average_count: float
    current_peak_count: int
    previous_average_count: float
    previous_peak_count: int
    average_change_percent: float | None

class TrafficComparisonReport(BaseModel):
    current_start_date: str
    current_end_date: str
    previous_start_date: str
    previous_end_date: str
    station_id: int | None
    line_name: str | None
    state: str | None
    stations: list[TrafficComparisonStation]
    generated_at: datetime
