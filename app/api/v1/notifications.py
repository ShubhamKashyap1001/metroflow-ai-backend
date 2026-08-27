"""Notification Center. Every authenticated user gets a feed of
what's happened in the last 7 days that's relevant to them - email
dispatch notices, operator-raised alerts, system announcements, and
system failure notices - behind the bell icon in the header.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.database.session import get_db
from app.enums.notification_source import NotificationSource
from app.models.user_profile import UserProfile
from app.schemas.notification import NotificationResponse, NotificationUnreadCount
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
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    """Last 7 days only - see NOTIFICATION_RETENTION_DAYS in
    app/services/notification_service.py. `state` narrows
    region-tagged rows to that state (untagged/global rows always
    still show) - same filter pattern as every other list endpoint."""
    return notification_service.list_notifications(
        db, current_user, source, unread_only, state
    )

@router.get("/unread-count", response_model=NotificationUnreadCount)
def get_unread_count(
    state: str | None = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return {"unread": notification_service.unread_count(db, current_user, state)}

@router.patch("/{notification_id}/read", response_model=NotificationResponse)
def read_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return notification_service.mark_read(db, notification_id, current_user)

@router.patch("/read-all", response_model=NotificationUnreadCount)
def read_all_notifications(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    notification_service.mark_all_read(db, current_user)
    return {"unread": 0}
