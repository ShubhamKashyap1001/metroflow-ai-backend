from datetime import datetime

from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import String

from sqlalchemy.dialects.postgresql import UUID

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base
from app.enums.notification_source import NotificationSource
from app.mixins.timestamp import TimestampMixin

class Notification(TimestampMixin, Base):

    __tablename__ = "notifications"

    __table_args__ = (
        Index("ix_notifications_created_at_is_read_user_id", "created_at", "is_read", "user_id"),
        Index("ix_notifications_binned_at", "binned_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_profiles.id"),
        nullable=True,
    )

    source: Mapped[NotificationSource] = mapped_column(
        Enum(NotificationSource)
    )

    title: Mapped[str] = mapped_column(
        String(200)
    )

    message: Mapped[str] = mapped_column(
        String(1000)
    )

    related_alert_id: Mapped[int | None] = mapped_column(
        ForeignKey("alerts.id"), nullable=True
    )

    state: Mapped[str | None] = mapped_column(String(50), nullable=True)

    is_read: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    binned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user = relationship("UserProfile")
