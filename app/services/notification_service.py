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
from app.models.notification_preference import NotificationPreference
from app.models.notification_read_state import NotificationReadState
from app.models.user_profile import UserProfile
from app.schemas.notification import NotificationResponse
from app.utils.geo import cities_for_state
from app.websocket import events

NOTIFICATION_RETENTION_DAYS = 7

DEFAULT_NOTIFICATIONS_LIMIT = 100
MAX_NOTIFICATIONS_LIMIT = 500

def _retention_cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=NOTIFICATION_RETENTION_DAYS)

def _bin_retention_cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(
        hours=settings.NOTIFICATION_BIN_RETENTION_HOURS
    )

def get_notification_preferences(db: Session, user: UserProfile) -> NotificationPreference:
    preferences = db.get(NotificationPreference, user.id)
    if preferences is None:
        preferences = NotificationPreference(user_id=user.id)
        db.add(preferences)
        db.commit()
        db.refresh(preferences)
    return preferences

def update_notification_preferences(
    db: Session, user: UserProfile, changes: dict
) -> NotificationPreference:
    preferences = get_notification_preferences(db, user)
    for field, value in changes.items():
        setattr(preferences, field, value)
    db.commit()
    db.refresh(preferences)
    return preferences

def _outerjoin_user_state(query, current_user: UserProfile, user_state):
    return query.outerjoin(
        user_state,
        and_(
            user_state.notification_id == Notification.id,
            user_state.user_id == current_user.id,
        ),
    )

def _effective_binned_at(user_state):
    return func.coalesce(user_state.binned_at, Notification.binned_at)

def _overlay_broadcast_state(
    db: Session, current_user: UserProfile, notifications: list[Notification]
) -> list[Notification]:
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

        just_read = _mark_broadcast_state(db, current_user, notification.id, "read_at")
        db.commit()

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

        manager.notify_user(
            str(current_user.id),
            events.NOTIFICATION_READ,
            {"id": notification.id},
        )
    return notification

def mark_all_read(db: Session, current_user: UserProfile) -> int:

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
    notification = db.get(Notification, notification_id)
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")

    is_mine = notification.user_id is None or str(notification.user_id) == str(current_user.id)
    if not is_mine:
        raise HTTPException(status_code=404, detail="Notification not found")

    if notification.user_id is None:
        _mark_broadcast_state(db, current_user, notification.id, "binned_at")
        db.commit()
    else:
        if notification.binned_at is None:
            notification.binned_at = datetime.now(timezone.utc)
            db.add(notification)
            db.commit()

    manager.notify_user(
        str(current_user.id),
        events.NOTIFICATION_DELETED,
        {"id": notification_id},
    )

def delete_all_notifications(db: Session, current_user: UserProfile) -> int:
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
