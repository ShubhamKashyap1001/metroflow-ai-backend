"""Notification Center. Every authenticated user gets a feed of
what's happened in the last 7 days that's relevant to them - email
dispatch notices, operator-raised alerts, system announcements, and
system failure notices - behind the bell icon in the header.
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.rate_limit import WRITE_LIMIT, limiter
from app.core.security import get_current_user
from app.database.session import get_db
from app.enums.notification_source import NotificationSource
from app.models.user_profile import UserProfile
from app.schemas.notification import NotificationResponse, NotificationUnreadCount, NotificationDeleteCount
from app.services import notification_service

router = APIRouter(
    prefix="/notifications",
    tags=["Notifications"]
)

@router.get("/", response_model=list[NotificationResponse])
def get_notifications(
    source: NotificationSource | None = None,
    unread_only: bool = False,
    state: str | None = None,
    limit: int = notification_service.DEFAULT_NOTIFICATIONS_LIMIT,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    """Last 7 days only - see NOTIFICATION_RETENTION_DAYS in
    app/services/notification_service.py. `state` narrows
    region-tagged rows to that state (untagged/global rows always
    still show) - same filter pattern as every other list endpoint.
    `limit`/`offset` page through results, most recent first (default
    page size and hard cap enforced server-side, so an out-of-range
    value here is clamped rather than rejected)."""
    return notification_service.list_notifications(
        db, current_user, source, unread_only, state, limit, offset
    )

@router.get("/bin", response_model=list[NotificationResponse])
def get_binned_notifications(
    limit: int = notification_service.DEFAULT_NOTIFICATIONS_LIMIT,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    """Notifications a "mark all as read" sweep or a per-item delete
    has moved out of the Inbox - see the Bin section of
    app/models/notification.py's docstring. Each one is hard-deleted
    NOTIFICATION_BIN_RETENTION_HOURS (72h) after it landed here by the
    background job in app/simulator/notification_bin_retention.py, so
    this list naturally empties out on its own."""
    return notification_service.list_binned_notifications(db, current_user, limit, offset)

@router.get("/unread-count", response_model=NotificationUnreadCount)
def get_unread_count(
    state: str | None = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return {"unread": notification_service.unread_count(db, current_user, state)}

@router.patch("/{notification_id}/read", response_model=NotificationResponse)
@limiter.limit(WRITE_LIMIT)
def read_notification(
    request: Request,
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return notification_service.mark_read(db, notification_id, current_user)

@router.delete("/{notification_id}", status_code=204)
@limiter.limit(WRITE_LIMIT)
def delete_notification(
    request: Request,
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    """Moves one specific notification straight to the Bin - the
    per-card delete button, as opposed to the "Delete All" sweep
    below, which is permanent right away."""
    notification_service.delete_notification(db, notification_id, current_user)

@router.delete("/", response_model=NotificationDeleteCount)
@limiter.limit(WRITE_LIMIT)
def delete_all_notifications(
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    """Wipes every notification this user can see - Inbox and Bin
    alike - in one go. Unlike a single-item delete or "mark all as
    read" (both of which land in the Bin first), this is permanent and
    immediate, no 72h grace period."""
    deleted = notification_service.delete_all_notifications(db, current_user)
    return {"deleted": deleted}

@router.patch("/read-all", response_model=NotificationUnreadCount)
@limiter.limit(WRITE_LIMIT)
def read_all_notifications(
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    """Marks every unread row read AND moves it to the Bin (see
    notification_service.mark_all_read) - so it disappears from the
    Inbox immediately instead of lingering there until the 7-day
    retention window catches up to it."""
    notification_service.mark_all_read(db, current_user)
    return {"unread": 0}