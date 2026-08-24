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

from app.enums.notification_source import NotificationSource
from app.models.notification import Notification
from app.models.user_profile import UserProfile

NOTIFICATION_RETENTION_DAYS = 7

def _retention_cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=NOTIFICATION_RETENTION_DAYS)

def list_notifications(
    db: Session,
    current_user: UserProfile,
    source: NotificationSource | None = None,
    unread_only: bool = False,
) -> list[Notification]:
    """Every user sees broadcast rows (user_id IS NULL - operator
    alerts, system announcements, system failures) plus any rows
    addressed to them specifically, from the last 7 days only."""
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

    return query.order_by(Notification.created_at.desc()).all()

def unread_count(db: Session, current_user: UserProfile) -> int:
    return (
        db.query(Notification)
        .filter(
            Notification.created_at >= _retention_cutoff(),
            Notification.is_read.is_(False),
            or_(
                Notification.user_id.is_(None),
                Notification.user_id == current_user.id,
            ),
        )
        .count()
    )

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
    return notification

def mark_all_read(db: Session, current_user: UserProfile) -> int:
    rows = list_notifications(db, current_user, unread_only=True)
    for row in rows:
        row.is_read = True
        db.add(row)
    db.commit()
    return len(rows)

def create_notification(
    db: Session,
    source: NotificationSource,
    title: str,
    message: str,
    user_id: UUID | str | None = None,
    related_alert_id: int | None = None,
) -> Notification:
    """Fire-and-forget helper used by other services (alert_service,
    news_service, and app/main.py's failure handlers) to drop a row
    into the bell feed. Broadcast (user_id=None) unless a specific
    user is given."""
    notification = Notification(
        user_id=user_id,
        source=source,
        title=title[:200],
        message=message[:1000],
        related_alert_id=related_alert_id,
    )
    db.add(notification)
    db.commit()
    db.refresh(notification)
    return notification
