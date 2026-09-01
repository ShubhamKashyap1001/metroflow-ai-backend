"""Service layer for the Notification Center (the bell icon).

Retention is enforced by never querying past NOTIFICATION_RETENTION_DAYS,
not by deleting rows - simplest possible "notifications older than 7
days disappear" behaviour, and it means nothing has to run a cron/
background job for it to work correctly.
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.websocket import manager
from app.enums.notification_source import NotificationSource
from app.models.notification import Notification
from app.models.user_profile import UserProfile
from app.schemas.notification import NotificationResponse
from app.utils.geo import cities_for_state
from app.websocket import events

NOTIFICATION_RETENTION_DAYS = 7

# Phase 9: the 7-day retention window bounds *how far back* a query
# looks, but not *how many rows* come back within that window - a busy
# deployment (or a broadcast-heavy day: several operator alerts, system
# announcements, etc. all fanned out to every user) can still put
# thousands of rows within the last 7 days, and list_notifications had
# no limit/offset at all. Same default-page + hard-cap pattern as
# alert_service.list_alerts.
DEFAULT_NOTIFICATIONS_LIMIT = 100
MAX_NOTIFICATIONS_LIMIT = 500

def _retention_cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=NOTIFICATION_RETENTION_DAYS)

def _bin_retention_cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(
        hours=settings.NOTIFICATION_BIN_RETENTION_HOURS
    )

def _notifications_query(
    db: Session,
    current_user: UserProfile,
    source: NotificationSource | None = None,
    unread_only: bool = False,
    state: str | None = None,
):
    """Shared filter logic for list_notifications/unread_count/
    mark_all_read - every user sees broadcast rows (user_id IS NULL -
    operator alerts, system announcements, system failures) plus any
    rows addressed to them specifically, from the last 7 days only.

    Always excludes binned rows (binned_at IS NOT NULL) - those moved
    out of the Inbox the moment a "mark all as read" sweep touched
    them and only show up via list_binned_notifications() now. See
    the Bin section of app/models/notification.py's docstring.

    When `state` is given, station-scoped notifications (Notification.
    state set) are further narrowed to that state - a row with
    state=NULL (not tied to any one state, e.g. a login notice or
    system announcement) always still shows regardless of the filter.
    """
    query = db.query(Notification).filter(
        Notification.created_at >= _retention_cutoff(),
        Notification.binned_at.is_(None),
        or_(
            Notification.user_id.is_(None),
            Notification.user_id == current_user.id,
        ),
    )

    if source:
        query = query.filter(Notification.source == source)
    if unread_only:
        query = query.filter(Notification.is_read.is_(False))
    if state:
        cities = cities_for_state(state) or [state]
        query = query.filter(
            or_(Notification.state.is_(None), Notification.state.in_(cities))
        )

    return query

def list_notifications(
    db: Session,
    current_user: UserProfile,
    source: NotificationSource | None = None,
    unread_only: bool = False,
    state: str | None = None,
    limit: int = DEFAULT_NOTIFICATIONS_LIMIT,
    offset: int = 0,
) -> list[Notification]:
    limit = min(max(limit or DEFAULT_NOTIFICATIONS_LIMIT, 1), MAX_NOTIFICATIONS_LIMIT)
    offset = max(offset or 0, 0)

    query = _notifications_query(db, current_user, source, unread_only, state)
    return (
        query.order_by(Notification.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

def list_binned_notifications(
    db: Session,
    current_user: UserProfile,
    limit: int = DEFAULT_NOTIFICATIONS_LIMIT,
    offset: int = 0,
) -> list[Notification]:
    """The Bin tab: everything a "mark all as read" sweep has moved
    out of the Inbox, still within NOTIFICATION_BIN_RETENTION_HOURS of
    the moment it was binned. Same broadcast-or-mine visibility as the
    Inbox (_notifications_query) - just filtered on binned_at instead
    of excluding it, and on the (much shorter) bin cutoff instead of
    the 7-day retention cutoff. The row disappears from here on its
    own once the background job in
    app/simulator/notification_bin_retention.py hard-deletes it; this
    extra cutoff filter is just a safety net so a row that's aged out
    but hasn't been swept yet (job interval, or the job being briefly
    down) doesn't linger visibly in the Bin past its 72h promise.
    """
    limit = min(max(limit or DEFAULT_NOTIFICATIONS_LIMIT, 1), MAX_NOTIFICATIONS_LIMIT)
    offset = max(offset or 0, 0)

    query = db.query(Notification).filter(
        Notification.binned_at.isnot(None),
        Notification.binned_at >= _bin_retention_cutoff(),
        or_(
            Notification.user_id.is_(None),
            Notification.user_id == current_user.id,
        ),
    )
    return (
        query.order_by(Notification.binned_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

def unread_count(
    db: Session, current_user: UserProfile, state: str | None = None
) -> int:
    # A COUNT query, not a row fetch - no pagination concern here.
    query = _notifications_query(db, current_user, unread_only=True, state=state)
    return query.count()

def mark_read(db: Session, notification_id: int, current_user: UserProfile) -> Notification:
    notification = db.get(Notification, notification_id)
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")

    is_mine = notification.user_id is None or str(notification.user_id) == str(current_user.id)
    if not is_mine:
        raise HTTPException(status_code=404, detail="Notification not found")

    if not notification.is_read:
        notification.is_read = True
        db.add(notification)
        db.commit()
        db.refresh(notification)
        # Sync read-state across this user's other open tabs/devices
        # instantly, instead of them waiting for the next poll.
        manager.notify_user(
            str(current_user.id),
            events.NOTIFICATION_READ,
            {"id": notification.id},
        )
    return notification

def mark_all_read(db: Session, current_user: UserProfile) -> int:
    # Deliberately NOT list_notifications(): marking "all" read has to
    # touch every unread row in the retention window, not just one
    # capped page of it, or unread_count would stay stuck above 0 after
    # the user hits "mark all read". This never serializes the rows to
    # a client, so the response-size limit that list_notifications
    # exists for doesn't apply here.
    #
    # Bulk UPDATE, not a load-then-loop: the previous version did
    # .all() on this query, pulling every matching Notification into
    # the ORM session as a full Python object just to flip two
    # columns and re-add() it - on a broadcast-heavy day that's
    # thousands of rows hydrated (and thousands of individual
    # UPDATEs autoflushed) for work a single set-based statement does
    # in the database. Query.update(synchronize_session=False) issues
    # one UPDATE ... WHERE (same filters _notifications_query already
    # applies, so it hits the existing ix_notifications_created_at_is_
    # read_user_id index the same way list_notifications/unread_count
    # do) and returns the matched row count directly - no ORM rows are
    # ever loaded, and the one statement + one commit stays a short,
    # bounded transaction instead of holding row locks open across N
    # separate Python-side mutations. `synchronize_session=False` is
    # safe here because nothing after this call touches those specific
    # Notification instances via `db`'s identity map.
    now = datetime.now(timezone.utc)
    updated = _notifications_query(db, current_user, unread_only=True).update(
        {
            Notification.is_read: True,
            # Bin it: this is what makes "mark all as read" actually
            # clear the Inbox (not just the unread badge) - the row
            # instantly stops matching _notifications_query (which
            # excludes binned rows) and starts showing up in the Bin
            # tab instead, until the background job in
            # app/simulator/notification_bin_retention.py
            # hard-deletes it NOTIFICATION_BIN_RETENTION_HOURS later.
            # A single item marked read via mark_read() is
            # deliberately left alone here - only the bulk sweep bins.
            Notification.binned_at: now,
        },
        synchronize_session=False,
    )
    db.commit()
    if updated:
        manager.notify_user(str(current_user.id), events.NOTIFICATION_ALL_READ, {})
    return updated

def delete_notification(db: Session, notification_id: int, current_user: UserProfile) -> None:
    """Permanently removes a single notification the moment the user
    asks for it - unlike mark_all_read, this doesn't wait out the Bin
    window first. Same broadcast-or-mine ownership check as
    mark_read/mark_all_read: a broadcast row (user_id NULL) has no
    per-user visibility flag on this table, so deleting it removes it
    for every user, not just the one who clicked delete - same
    trade-off the existing mark_read/mark_all_read already make."""
    notification = db.get(Notification, notification_id)
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")

    is_mine = notification.user_id is None or str(notification.user_id) == str(current_user.id)
    if not is_mine:
        raise HTTPException(status_code=404, detail="Notification not found")

    db.delete(notification)
    db.commit()
    manager.notify_user(
        str(current_user.id),
        events.NOTIFICATION_DELETED,
        {"id": notification_id},
    )

def delete_all_notifications(db: Session, current_user: UserProfile) -> int:
    """Hard-deletes every notification visible to this user - both
    whatever's currently in the Inbox and anything already sitting in
    the Bin - not just one page of it. Deliberately not scoped by the
    7-day retention cutoff or binned_at the way _notifications_query
    is: "Delete All" is meant to actually clear everything the user
    can see, not just what a filtered list would currently return.
    Same broadcast-or-mine visibility rule as everywhere else in this
    module, and the same bulk-statement approach as mark_all_read
    (one DELETE ... WHERE, no per-row ORM loop)."""
    deleted = db.query(Notification).filter(
        or_(
            Notification.user_id.is_(None),
            Notification.user_id == current_user.id,
        )
    ).delete(synchronize_session=False)
    db.commit()
    if deleted:
        manager.notify_user(str(current_user.id), events.NOTIFICATION_ALL_DELETED, {})
    return deleted

def create_notification(
    db: Session,
    source: NotificationSource,
    title: str,
    message: str,
    user_id: UUID | str | None = None,
    related_alert_id: int | None = None,
    state: str | None = None,
) -> Notification:
    """Fire-and-forget helper used by other services (alert_service,
    news_service, and app/main.py's failure handlers) to drop a row
    into the bell feed. Broadcast (user_id=None) unless a specific
    user is given.

    `state` (e.g. "West Bengal") ties a broadcast row to one region -
    callers that know which station/city this is about should resolve
    it via app/utils/geo.py::state_for_city and pass it through, so
    the frontend only surfaces it to users who have that state
    selected. Leave it None for anything that isn't region-specific
    (system announcements, login notices, failures) - those still
    reach everyone.

    After committing, also pushes the same row over the live socket
    (as a "notification" event) so an open tab gets it instantly
    instead of waiting for the next 30s poll: broadcast to everyone if
    user_id is None, or targeted at just that user's connection(s)
    otherwise. Push-on-top-of-pull - an offline/disconnected recipient
    still sees it next time they load or poll the feed, since the row
    is already committed."""
    notification = Notification(
        user_id=user_id,
        source=source,
        title=title[:200],
        message=message[:1000],
        related_alert_id=related_alert_id,
        state=state,
    )
    db.add(notification)
    db.commit()
    db.refresh(notification)

    payload = NotificationResponse.model_validate(notification).model_dump(mode="json")
    if notification.user_id is None:
        manager.notify(events.NOTIFICATION, payload)
    else:
        manager.notify_user(str(notification.user_id), events.NOTIFICATION, payload)

    return notification