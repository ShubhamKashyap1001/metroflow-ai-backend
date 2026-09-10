"""Per-user overlay state for broadcast notifications: read, bin, and
delete.

Three shared columns on `Notification` (app/models/notification.py) -
`is_read`, `binned_at`, and hard deletion of the row itself - are
correct for a personal notification (`user_id` set - only one user can
ever see it), but wrong for a broadcast notification (`user_id IS
NULL` - operator alerts, system announcements, system failures):
flipping/deleting the one shared row affects *every* user who sees the
feed, not just whoever acted on it.

This table holds at most one row per (user, notification), created the
moment that specific user first reads, bins, or deletes a broadcast
notification. Each of the three timestamp columns is independently
nullable - a row can represent "read but not binned", "binned and
deleted", etc. - and NULL always means "this user hasn't done that
action", identical to the pre-existing behaviour. Personal
notifications never get a row here; they keep using `Notification.
is_read`/`binned_at`/row-deletion exactly as before (see
app/services/notification_service.py).
"""
from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import UniqueConstraint

from sqlalchemy.dialects.postgresql import UUID

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from app.database.base import Base

class NotificationReadState(Base):

    __tablename__ = "notification_read_states"

    __table_args__ = (
        UniqueConstraint("user_id", "notification_id", name="ux_notification_read_states_user_notification"),
        Index("ix_notification_read_states_notification_id", "notification_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_profiles.id"),
        nullable=False,
    )

    notification_id: Mapped[int] = mapped_column(
        ForeignKey("notifications.id"),
        nullable=False,
    )

    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Set the moment THIS user bins the broadcast row (individually or
    # via "mark all as read") - mirrors Notification.binned_at but
    # scoped to one user instead of hiding it from everyone's Inbox.
    binned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Set the moment THIS user hits "Delete All" and it sweeps up this
    # broadcast row (see notification_service.delete_all_notifications)
    # - the row itself is never removed (other users still see it),
    # this just hides it from this user's Inbox and Bin permanently,
    # skipping the Bin entirely. A per-card delete on a single
    # broadcast row uses `binned_at` above instead, same as "mark all
    # as read", so it still goes through the Bin first.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

