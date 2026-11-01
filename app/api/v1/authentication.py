from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core import cache
from app.core.rate_limit import AUTH_LIMIT, limiter
from app.core.security import get_current_user
from app.database.session import get_db
from app.enums.notification_source import NotificationSource
from app.models.user_profile import UserProfile
from app.schemas.user_profile import UserProfileResponse
from app.services import notification_service

router = APIRouter(
    prefix="/auth",
    tags=["Authentication"]
)

LOGIN_NOTIFICATION_COOLDOWN_SECONDS = 6 * 60 * 60

def _maybe_notify_login(db: Session, user: UserProfile) -> None:
    cache_key = f"login-notified:{user.id}"
    if cache.get_json(cache_key) is not None:
        return
    cache.set_json(cache_key, True, ttl_seconds=LOGIN_NOTIFICATION_COOLDOWN_SECONDS)
    notification_service.create_notification(
        db,
        source=NotificationSource.SYSTEM,
        title="New login",
        message=f"Welcome back, {user.full_name or user.email or 'there'} - you're logged in to MetroFlow.",
        user_id=user.id,
    )

@router.get("/me", response_model=UserProfileResponse)
@limiter.limit(AUTH_LIMIT)
def read_current_user(
    request: Request,
    current_user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _maybe_notify_login(db, current_user)
    return current_user
