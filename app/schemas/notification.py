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
    # Set the moment a "mark all as read" sweep bins this row (see
    # notification_service.mark_all_read) - null means it's still in
    # the Inbox. The frontend uses this to show a "removes in Xh" hint
    # on the Bin tab (binned_at + NOTIFICATION_BIN_RETENTION_HOURS).
    binned_at: datetime | None = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class NotificationUnreadCount(BaseModel):
    unread: int

class NotificationDeleteCount(BaseModel):
    deleted: int