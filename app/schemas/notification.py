from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict

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
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class NotificationUnreadCount(BaseModel):
    unread: int
