"""Notification Center module - what the bell icon in the header opens.

Distinct from `NotificationLog` (app/models/notification_log.py),
which is a low-level per-recipient email/SMS delivery audit row for a
single Alert. This table is the human-facing feed: one row per thing
worth showing a user in their bell dropdown, tagged by `source` so the
UI can filter/tab between them:

  - EMAIL           an email notification was dispatched (see
                     app/services/alert_service.py's dispatch calls)
  - OPERATOR        an operator/admin raised a station alert
                     (overcrowding/delay/emergency/maintenance/info)
  - SYSTEM          a system-level announcement - e.g. a published
                     News item (see app/services/news_service.py)
  - SYSTEM_FAILURE  an infrastructure problem the backend itself
                     detected - e.g. the DB pool exhaustion handler in
                     app/main.py

`user_id = NULL` means "broadcast to everyone" (used for operator
alerts, system announcements, and system failures - anything that
isn't specific to one person). A non-null `user_id` scopes it to that
one user only.

Retention is enforced at read time, not by deleting rows:
list_notifications() only ever returns rows from the last
`NOTIFICATION_RETENTION_DAYS` (7) days - see
app/services/notification_service.py.
"""
from sqlalchemy import Boolean
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
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

    # Which state/region this notification is about (e.g. "West Bengal"
    # for a Kolkata station), resolved from the station's city via
    # app/utils/geo.py at creation time - NULL means "not tied to any
    # one state" (system announcements, login notices, etc.) and is
    # always shown regardless of the user's selected state filter.
    # See app/services/notification_service.py::create_notification.
    state: Mapped[str | None] = mapped_column(String(50), nullable=True)

    is_read: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    user = relationship("UserProfile")
