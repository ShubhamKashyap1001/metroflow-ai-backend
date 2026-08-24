from datetime import datetime

from pydantic import BaseModel
from pydantic import ConfigDict

from app.enums.notification_channel import NotificationChannel
from app.enums.notification_status import NotificationStatus

class NotificationLogResponse(BaseModel):
    id: int
    alert_id: int
    channel: NotificationChannel
    recipient: str
    status: NotificationStatus
    error_message: str | None
    sent_at: datetime | None
    created_at: datetime
    model_config = ConfigDict(
        from_attributes=True
    )
