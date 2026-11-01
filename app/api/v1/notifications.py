from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.rate_limit import WRITE_LIMIT, limiter
from app.core.security import get_current_user
from app.database.session import get_db
from app.enums.notification_source import NotificationSource
from app.models.user_profile import UserProfile
from app.schemas.notification import (
    NotificationDeleteCount,
    NotificationPreferencesResponse,
    NotificationPreferencesUpdate,
    NotificationResponse,
    NotificationUnreadCount,
)
from app.services import notification_service

router = APIRouter(
    prefix="/notifications",
    tags=["Notifications"]
)

@router.get("/preferences", response_model=NotificationPreferencesResponse)
def get_preferences(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return notification_service.get_notification_preferences(db, current_user)

@router.patch("/preferences", response_model=NotificationPreferencesResponse)
@limiter.limit(WRITE_LIMIT)
def update_preferences(
    request: Request,
    payload: NotificationPreferencesUpdate,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    changes = payload.model_dump(exclude_unset=True)
    return notification_service.update_notification_preferences(db, current_user, changes)

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
    notification_service.delete_notification(db, notification_id, current_user)

@router.delete("/", response_model=NotificationDeleteCount)
@limiter.limit(WRITE_LIMIT)
def delete_all_notifications(
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    deleted = notification_service.delete_all_notifications(db, current_user)
    return {"deleted": deleted}

@router.patch("/read-all", response_model=NotificationUnreadCount)
@limiter.limit(WRITE_LIMIT)
def read_all_notifications(
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    notification_service.mark_all_read(db, current_user)
    return {"unread": 0}
