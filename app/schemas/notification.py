from datetime import datetime
import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.enums.notification_source import NotificationSource

class NotificationResponse(BaseModel):
    id: int
    user_id: UUID | None
    source: NotificationSource
    title: str
    message: str
    related_alert_id: int | None
    state: str | None = None
    is_read: bool

    binned_at: datetime | None = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class NotificationUnreadCount(BaseModel):
    unread: int

class NotificationDeleteCount(BaseModel):
    deleted: int

class NotificationPreferencesUpdate(BaseModel):
    browser_notifications_enabled: bool | None = None
    quiet_hours_enabled: bool | None = None
    quiet_hours_start: str | None = Field(default=None, min_length=5, max_length=5)
    quiet_hours_end: str | None = Field(default=None, min_length=5, max_length=5)

    @field_validator("quiet_hours_start", "quiet_hours_end")
    @classmethod
    def validate_clock_time(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Use a 24-hour time in HH:MM format.")
        return value

class NotificationPreferencesResponse(BaseModel):
    browser_notifications_enabled: bool
    quiet_hours_enabled: bool
    quiet_hours_start: str
    quiet_hours_end: str
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)
