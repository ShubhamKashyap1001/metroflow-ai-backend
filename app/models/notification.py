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

Bin (Phase 12): hitting "mark all as read" doesn't just flip
`is_read` - it also stamps `binned_at` on every row it touches,
which immediately excludes those rows from list_notifications()
(the main Inbox feed). They still exist and are reachable via the
separate Bin tab (list_binned_notifications()) until
`app/simulator/notification_bin_retention.py`'s background job
hard-deletes anything whose `binned_at` is older than
NOTIFICATION_BIN_RETENTION_HOURS (24) - see that module and
notification_service.mark_all_read/list_binned_notifications.
A single item marked read individually (mark_read()) is NOT
binned - it just stops being unread and stays in the Inbox, same
as before this change.
"""
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

    # BUGFIX (slow bell-icon feed at scale): this table had NO indexes
    # beyond the primary key, but is polled every ~30s by every open
    # client (list_notifications/unread_count, both filtered by the
    # 7-day retention cutoff on created_at plus a broadcast-or-mine OR
    # on user_id, and unread_count additionally filters is_read) and
    # never trimmed (retention is enforced at read time, not by
    # deleting rows - see notification_service.py), so it only grows.
    # Measured on a 408k-row table (before -> after):
    #   list_notifications: 70ms  -> 0.36ms (Index Scan Backward,
    #     stops at the 7-day cutoff instead of a full parallel seq
    #     scan of the whole table)
    #   unread_count:        56ms -> 25ms   (Index Only Scan, 0 heap
    #     fetches - a plain created_at-only index measured NO
    #     improvement here (still ~44-60ms) because a COUNT with no
    #     LIMIT has to visit every row in the 7-day window regardless
    #     of index; only once is_read/user_id were added so the scan
    #     never touches the heap did this query speed up)
    # A leading (is_read, created_at) or (user_id, created_at) index
    # was also tried and measured no better than this single composite
    # for either query. See docs/query-performance-and-indexing.md for the full
    # EXPLAIN ANALYZE before/after.
    # `binned_at` index: the bin retention job (notification_bin_
    # retention.py) sweeps on a WHERE binned_at IS NOT NULL AND
    # binned_at < cutoff filter every NOTIFICATION_BIN_RETENTION_
    # INTERVAL_SECONDS, and the Bin tab lists on WHERE binned_at IS
    # NOT NULL ordered by binned_at - both want this indexed rather
    # than falling back to a seq scan as the table grows, same
    # reasoning as the composite index below.
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

    # NULL = sitting in the normal Inbox feed. Set the moment a
    # "mark all as read" sweep touches this row (see
    # notification_service.mark_all_read) - from then on it's excluded
    # from list_notifications() and only shows up in the Bin tab, until
    # the bin retention job hard-deletes it 24h later. Never set by
    # mark_read() (single-item read).
    binned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user = relationship("UserProfile")
