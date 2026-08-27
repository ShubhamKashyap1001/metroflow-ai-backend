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

from app.core.websocket import manager
from app.enums.notification_source import NotificationSource
from app.models.notification import Notification
from app.models.user_profile import UserProfile
from app.schemas.notification import NotificationResponse
from app.utils.geo import cities_for_state
from app.websocket import events

NOTIFICATION_RETENTION_DAYS = 7

def _retention_cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=NOTIFICATION_RETENTION_DAYS)

def list_notifications(
    db: Session,
    current_user: UserProfile,
    source: NotificationSource | None = None,
    unread_only: bool = False,
    state: str | None = None,
) -> list[Notification]:
    """Every user sees broadcast rows (user_id IS NULL - operator
    alerts, system announcements, system failures) plus any rows
    addressed to them specifically, from the last 7 days only.

    When `state` is given, station-scoped notifications (Notification.
    state set) are further narrowed to that state - a row with
    state=NULL (not tied to any one state, e.g. a login notice or
    system announcement) always still shows regardless of the filter.
    """
    query = db.query(Notification).filter(
        Notification.created_at >= _retention_cutoff(),
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

    return query.order_by(Notification.created_at.desc()).all()

def unread_count(
    db: Session, current_user: UserProfile, state: str | None = None
) -> int:
    query = db.query(Notification).filter(
        Notification.created_at >= _retention_cutoff(),
        Notification.is_read.is_(False),
        or_(
            Notification.user_id.is_(None),
            Notification.user_id == current_user.id,
        ),
    )
    if state:
        cities = cities_for_state(state) or [state]
        query = query.filter(
            or_(Notification.state.is_(None), Notification.state.in_(cities))
        )
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
    rows = list_notifications(db, current_user, unread_only=True)
    for row in rows:
        row.is_read = True
        db.add(row)
    db.commit()
    if rows:
        manager.notify_user(str(current_user.id), events.NOTIFICATION_ALL_READ, {})
    return len(rows)

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
