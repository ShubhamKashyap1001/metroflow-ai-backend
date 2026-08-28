from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import String

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.enums.notification_channel import NotificationChannel
from app.enums.notification_status import NotificationStatus
from app.mixins.timestamp import TimestampMixin

class NotificationLog(TimestampMixin, Base):

    __tablename__ = "notification_logs"

    id: Mapped[int] = mapped_column(primary_key=True)

    alert_id: Mapped[int] = mapped_column(
        ForeignKey("alerts.id")
    )

    channel: Mapped[NotificationChannel] = mapped_column(
        Enum(NotificationChannel),
        default=NotificationChannel.EMAIL,
    )

    recipient: Mapped[str] = mapped_column(
        String(255)
    )

    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus)
    )

    error_message: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )

    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    alert = relationship("Alert")
