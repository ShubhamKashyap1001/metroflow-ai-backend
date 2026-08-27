"""Auth itself (sign up / login / password reset / sessions) is fully
handled by Supabase on the frontend - see the frontend's
`src/lib/supabase/client.ts` and `src/app/(auth)/login/page.tsx`.

This backend only ever verifies the Supabase-issued JWT sent in the
`Authorization: Bearer <token>` header and exposes the resulting
profile. There is no /register or /login endpoint here on purpose.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core import cache
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

# The frontend calls GET /auth/me on every app load, not just a true
# sign-in - so "notify on every /me call" would fire on every page
# refresh. This cooldown makes it behave like a real "welcome back"
# ping instead: once per window per user, even across many page loads.
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
def read_current_user(
    current_user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Verifies the Supabase session token and returns (creating on
    first call) the matching app profile - role, name, etc."""
    _maybe_notify_login(db, current_user)
    return current_user
