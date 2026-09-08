"""Service layer for the Notification Center (the bell icon).

Retention is enforced by never querying past NOTIFICATION_RETENTION_DAYS,
not by deleting rows - simplest possible "notifications older than 7
days disappear" behaviour, and it means nothing has to run a cron/
background job for it to work correctly.
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased

from app.core.config import settings
from app.core.websocket import manager
from app.enums.notification_source import NotificationSource
from app.models.notification import Notification
from app.models.notification_read_state import NotificationReadState
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

def _outerjoin_user_state(query, current_user: UserProfile, user_state):
    """Left-joins this user's own NotificationReadState row (if any)
    onto a Notification query. A broadcast notification (user_id IS
    NULL) has no per-user read/bin/delete column of its own - this
    join is how every query below answers "is THIS row read/binned/
    deleted for THIS user" without touching the shared row, which is
    what previously let one user's action affect every other user
    who can see the same broadcast notification. Personal
    notifications (user_id set) never get a matching row here - they
    keep using Notification.is_read/binned_at/row-deletion directly,
    exactly as before."""
    return query.outerjoin(
        user_state,
        and_(
            user_state.notification_id == Notification.id,
            user_state.user_id == current_user.id,
        ),
    )

def _effective_binned_at(user_state):
    """The row's binned-for-this-user timestamp: the per-user overlay
    value for a broadcast row, or the shared column for a personal
    row. Only one of the two is ever non-NULL for a given row (a
    personal notification never gets an overlay row; a broadcast
    notification's shared `binned_at` is no longer written to), so a
    plain COALESCE picks whichever one applies."""
    return func.coalesce(user_state.binned_at, Notification.binned_at)

def _overlay_broadcast_state(
    db: Session, current_user: UserProfile, notifications: list[Notification]
) -> list[Notification]:
    """Fixes up the in-memory `is_read`/`binned_at` on broadcast rows
    to reflect THIS user's own overlay state (NotificationReadState)
    instead of the shared columns, right before handing them to the
    response schema. Each touched instance is expunged from the
    session afterward so this per-user overlay is never accidentally
    flushed back to the shared row (which would reintroduce the exact
    bug - one user's action changing what every other user sees - this
    exists to fix)."""
    broadcast_ids = [n.id for n in notifications if n.user_id is None]
    if not broadcast_ids:
        return notifications

    states = {
        row.notification_id: row
        for row in db.query(NotificationReadState)
        .filter(
            NotificationReadState.user_id == current_user.id,
            NotificationReadState.notification_id.in_(broadcast_ids),
        )
        .all()
    }
    for notification in notifications:
        if notification.user_id is None:
            row = states.get(notification.id)
            notification.is_read = row.read_at is not None if row else False
            notification.binned_at = row.binned_at if row else None
            db.expunge(notification)
    return notifications

def _mark_broadcast_state(
    db: Session, current_user: UserProfile, notification_id: int, column: str
) -> bool:
    """Sets NotificationReadState.<column> = now() for (current_user,
    notification_id), creating the overlay row first if none exists
    yet. Two statements rather than one UPSERT, because a row may
    already exist here from a *different* action (e.g. this
    notification was already deleted-for-me when the user separately
    marks it read) - `on_conflict_do_nothing` alone would then skip
    touching `column` entirely. Returns True the moment `column`
    newly gets a value; a repeat call for an already-set column
    changes nothing and returns False, so callers can skip
    re-broadcasting the same side effect (double-click, retried
    request)."""
    now = datetime.now(timezone.utc)
    insert_stmt = pg_insert(NotificationReadState).values(
        user_id=current_user.id,
        notification_id=notification_id,
        **{column: now},
    ).on_conflict_do_nothing(
        index_elements=[NotificationReadState.user_id, NotificationReadState.notification_id]
    )
    if db.execute(insert_stmt).rowcount == 1:
        return True

    result = db.execute(
        update(NotificationReadState)
        .where(
            NotificationReadState.user_id == current_user.id,
            NotificationReadState.notification_id == notification_id,
            getattr(NotificationReadState, column).is_(None),
        )
        .values(**{column: now})
    )
    return result.rowcount == 1

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

    Always excludes rows binned or deleted for THIS user - see
    _effective_binned_at and _outerjoin_user_state above. Those moved
    out of the Inbox the moment a "mark all as read" sweep (bin) or a
    delete touched them for this user specifically, and only show up
    via list_binned_notifications() now (bin) or nowhere at all
    (delete) - without affecting any other user's copy of the same
    broadcast row. See the Bin section of app/models/notification.py's
    docstring.

    When `state` is given, station-scoped notifications (Notification.
    state set) are further narrowed to that state - a row with
    state=NULL (not tied to any one state, e.g. a login notice or
    system announcement) always still shows regardless of the filter.
    """
    user_state = aliased(NotificationReadState)
    query = _outerjoin_user_state(db.query(Notification), current_user, user_state).filter(
        Notification.created_at >= _retention_cutoff(),
        _effective_binned_at(user_state).is_(None),
        or_(Notification.user_id.isnot(None), user_state.deleted_at.is_(None)),
        or_(
            Notification.user_id.is_(None),
            Notification.user_id == current_user.id,
        ),
    )

    if source:
        query = query.filter(Notification.source == source)
    if unread_only:
        query = query.filter(
            or_(
                and_(Notification.user_id.isnot(None), Notification.is_read.is_(False)),
                and_(Notification.user_id.is_(None), user_state.read_at.is_(None)),
            )
        )
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
    notifications = (
        query.order_by(Notification.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return _overlay_broadcast_state(db, current_user, notifications)

def list_binned_notifications(
    db: Session,
    current_user: UserProfile,
    limit: int = DEFAULT_NOTIFICATIONS_LIMIT,
    offset: int = 0,
) -> list[Notification]:
    """The Bin tab: everything a "mark all as read" sweep has moved
    out of the Inbox for THIS user, still within
    NOTIFICATION_BIN_RETENTION_HOURS of the moment it was binned. Same
    broadcast-or-mine visibility as the Inbox (_notifications_query) -
    just filtered on the row being binned-for-this-user instead of
    excluding it, and on the (much shorter) bin cutoff instead of the
    7-day retention cutoff. A personal row disappears from here on its
    own once the background job in
    app/simulator/notification_bin_retention.py hard-deletes it (it
    only ever looks at the shared column, which is exclusively used by
    personal rows now); a broadcast row's per-user overlay entry
    simply stops matching the cutoff filter below once it ages out.
    """
    limit = min(max(limit or DEFAULT_NOTIFICATIONS_LIMIT, 1), MAX_NOTIFICATIONS_LIMIT)
    offset = max(offset or 0, 0)

    user_state = aliased(NotificationReadState)
    binned_at = _effective_binned_at(user_state)
    query = _outerjoin_user_state(db.query(Notification), current_user, user_state).filter(
        binned_at.isnot(None),
        binned_at >= _bin_retention_cutoff(),
        or_(Notification.user_id.isnot(None), user_state.deleted_at.is_(None)),
        or_(
            Notification.user_id.is_(None),
            Notification.user_id == current_user.id,
        ),
    )
    notifications = (
        query.order_by(binned_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return _overlay_broadcast_state(db, current_user, notifications)

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

    if notification.user_id is None:
        # Broadcast row: record THIS user's read in NotificationReadState
        # instead of flipping the shared Notification.is_read column,
        # which used to mark it read for every user who can see the
        # feed.
        just_read = _mark_broadcast_state(db, current_user, notification.id, "read_at")
        db.commit()
        # Reflect this user's own read state in the returned object
        # without touching the shared row other users will read next -
        # expunge so this in-memory-only value can never be flushed.
        notification.is_read = True
        db.expunge(notification)
        if just_read:
            manager.notify_user(
                str(current_user.id),
                events.NOTIFICATION_READ,
                {"id": notification.id},
            )
        return notification

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
    # Personal rows: one bulk UPDATE, same as before - the shared
    # column is fine here since only this user ever owns these rows.
    now = datetime.now(timezone.utc)
    personal_updated = (
        db.query(Notification)
        .filter(
            Notification.user_id == current_user.id,
            Notification.created_at >= _retention_cutoff(),
            Notification.binned_at.is_(None),
            Notification.is_read.is_(False),
        )
        .update(
            {Notification.is_read: True, Notification.binned_at: now},
            synchronize_session=False,
        )
    )

    # Broadcast rows: can't bulk-UPDATE a shared column for these -
    # that's exactly the "affects every user" bug this fix closes.
    # Upsert one NotificationReadState row per currently-unread,
    # not-yet-deleted-for-me broadcast notification instead: insert
    # rows for whichever don't have one yet, then fill in read_at/
    # binned_at on any that already existed (e.g. a race with another
    # action) but hadn't been read yet.
    user_state = aliased(NotificationReadState)
    broadcast_ids = [
        row[0]
        for row in db.query(Notification.id)
        .outerjoin(
            user_state,
            and_(
                user_state.notification_id == Notification.id,
                user_state.user_id == current_user.id,
            ),
        )
        .filter(
            Notification.user_id.is_(None),
            Notification.created_at >= _retention_cutoff(),
            user_state.read_at.is_(None),
            user_state.deleted_at.is_(None),
        )
        .all()
    ]

    broadcast_updated = 0
    if broadcast_ids:
        insert_stmt = pg_insert(NotificationReadState).values([
            {"user_id": current_user.id, "notification_id": nid, "read_at": now, "binned_at": now}
            for nid in broadcast_ids
        ]).on_conflict_do_nothing(
            index_elements=[NotificationReadState.user_id, NotificationReadState.notification_id]
        )
        inserted = db.execute(insert_stmt).rowcount
        if inserted < len(broadcast_ids):
            db.execute(
                update(NotificationReadState)
                .where(
                    NotificationReadState.user_id == current_user.id,
                    NotificationReadState.notification_id.in_(broadcast_ids),
                    NotificationReadState.read_at.is_(None),
                )
                .values(read_at=now, binned_at=now)
            )
        broadcast_updated = len(broadcast_ids)

    db.commit()
    updated = personal_updated + broadcast_updated
    if updated:
        manager.notify_user(str(current_user.id), events.NOTIFICATION_ALL_READ, {})
    return updated

def delete_notification(db: Session, notification_id: int, current_user: UserProfile) -> None:
    """Removes a single notification the moment the user asks for it -
    unlike mark_all_read, this doesn't wait out the Bin window first.
    Same broadcast-or-mine ownership check as mark_read/mark_all_read.
    A personal row is hard-deleted (only this user ever owns it, so
    that's already per-user). A broadcast row (user_id NULL) is never
    removed or altered - deleting it records a per-user `deleted_at`
    overlay instead, so it disappears from this user's Inbox/Bin only
    and every other user's copy is untouched."""
    notification = db.get(Notification, notification_id)
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")

    is_mine = notification.user_id is None or str(notification.user_id) == str(current_user.id)
    if not is_mine:
        raise HTTPException(status_code=404, detail="Notification not found")

    if notification.user_id is None:
        _mark_broadcast_state(db, current_user, notification.id, "deleted_at")
        db.commit()
    else:
        db.delete(notification)
        db.commit()

    manager.notify_user(
        str(current_user.id),
        events.NOTIFICATION_DELETED,
        {"id": notification_id},
    )

def delete_all_notifications(db: Session, current_user: UserProfile) -> int:
    """Removes every notification visible to this user - both
    whatever's currently in the Inbox and anything already sitting in
    the Bin - not just one page of it. Deliberately not scoped by the
    7-day retention cutoff or binned_at the way _notifications_query
    is: "Delete All" is meant to actually clear everything the user
    can see, not just what a filtered list would currently return.
    Personal rows are hard-deleted (one bulk DELETE, only this user
    ever owns them). Broadcast rows are never removed - each gets a
    per-user `deleted_at` overlay instead, same as delete_notification,
    so this only clears the current user's own view."""
    personal_deleted = (
        db.query(Notification)
        .filter(Notification.user_id == current_user.id)
        .delete(synchronize_session=False)
    )

    now = datetime.now(timezone.utc)
    user_state = aliased(NotificationReadState)
    broadcast_ids = [
        row[0]
        for row in db.query(Notification.id)
        .outerjoin(
            user_state,
            and_(
                user_state.notification_id == Notification.id,
                user_state.user_id == current_user.id,
            ),
        )
        .filter(
            Notification.user_id.is_(None),
            user_state.deleted_at.is_(None),
        )
        .all()
    ]

    broadcast_deleted = 0
    if broadcast_ids:
        insert_stmt = pg_insert(NotificationReadState).values([
            {"user_id": current_user.id, "notification_id": nid, "deleted_at": now}
            for nid in broadcast_ids
        ]).on_conflict_do_nothing(
            index_elements=[NotificationReadState.user_id, NotificationReadState.notification_id]
        )
        inserted = db.execute(insert_stmt).rowcount
        if inserted < len(broadcast_ids):
            db.execute(
                update(NotificationReadState)
                .where(
                    NotificationReadState.user_id == current_user.id,
                    NotificationReadState.notification_id.in_(broadcast_ids),
                    NotificationReadState.deleted_at.is_(None),
                )
                .values(deleted_at=now)
            )
        broadcast_deleted = len(broadcast_ids)

    db.commit()
    deleted = personal_deleted + broadcast_deleted
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
