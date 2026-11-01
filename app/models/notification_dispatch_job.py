from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from app.database.base import Base
from app.enums.notification_dispatch_kind import NotificationDispatchKind
from app.enums.notification_dispatch_status import NotificationDispatchStatus
from app.mixins.timestamp import TimestampMixin

class NotificationDispatchJob(TimestampMixin, Base):

    __tablename__ = "notification_dispatch_jobs"

    __table_args__ = (
        Index("ix_notification_dispatch_jobs_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    kind: Mapped[NotificationDispatchKind] = mapped_column(
        Enum(NotificationDispatchKind)
    )

    alert_id: Mapped[int] = mapped_column(
        ForeignKey("alerts.id")
    )

    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    notify_email: Mapped[bool] = mapped_column(default=False)
    notify_sms: Mapped[bool] = mapped_column(default=False)

    status: Mapped[NotificationDispatchStatus] = mapped_column(
        Enum(NotificationDispatchStatus),
        default=NotificationDispatchStatus.QUEUED,
    )

    attempts: Mapped[int] = mapped_column(Integer, default=0)

    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
