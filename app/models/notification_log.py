"""Per-recipient delivery record for the Alert & Notification Module.

One row per (alert, channel, recipient) triple, so the Alerts page can
show exactly who was notified on which channel (email/SMS), who
wasn't, and why - instead of the create endpoint silently succeeding
or failing on dispatch with no audit trail.
"""
from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import Index
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

    # BUGFIX (slow per-alert delivery log lookup): no index on
    # alert_id (a plain FK column is NOT auto-indexed by Postgres).
    # list_alert_notifications() filters by alert_id and orders by
    # created_at on every "Alerts -> view delivery log" click - this
    # table grows one row per (alert, channel, recipient), so it's
    # the fastest-growing table in the schema (a single alert emailed
    # to a large active user base is thousands of rows by itself).
    # Measured on a 606k-row table: 40ms full seq scan -> 0.04ms
    # index scan for a single alert's log (verified with EXPLAIN
    # ANALYZE before/after - see docs/query-performance-and-indexing.md).
    __table_args__ = (
        Index("ix_notification_logs_alert_id_created_at", "alert_id", "created_at"),
    )

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
