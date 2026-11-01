from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.rate_limit import WRITE_LIMIT, limiter
from app.core.security import get_current_user, invalidate_user_cache, require_roles
from app.database.session import get_db
from app.enums.user_role import UserRole
from app.models.user_profile import UserProfile
from app.schemas.user_profile import UserProfileResponse, UserProfileUpdate
from app.simulator.constants import SIMULATED_EMAIL_DOMAIN

router = APIRouter(
    prefix="/users",
    tags=["Users"]
)

DEFAULT_USERS_LIMIT = 100
MAX_USERS_LIMIT = 500

def _clamp(value: int | None, default: int, maximum: int) -> int:
    if value is None:
        value = default
    return min(max(value, 1), maximum)

@router.get("/", response_model=list[UserProfileResponse])
def get_users(
    limit: int = DEFAULT_USERS_LIMIT,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN)),
):
    limit = _clamp(limit, DEFAULT_USERS_LIMIT, MAX_USERS_LIMIT)
    offset = max(offset or 0, 0)
    return (
        db.query(UserProfile)
        .filter(~UserProfile.email.like(f"%@{SIMULATED_EMAIL_DOMAIN}"))
        .order_by(UserProfile.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

@router.get("/{user_id}", response_model=UserProfileResponse)
def get_user(
    user_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    user = db.get(UserProfile, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user

@router.put("/{user_id}", response_model=UserProfileResponse)
@limiter.limit(WRITE_LIMIT)
def update_user(
    request: Request,
    user_id: UUID,
    payload: UserProfileUpdate,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    if current_user.role != UserRole.ADMIN and current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Not allowed to edit this profile")

    user = db.get(UserProfile, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    update_data = payload.model_dump(exclude_unset=True)

    if current_user.role != UserRole.ADMIN:
        update_data.pop("role", None)
        update_data.pop("is_active", None)

    for field, value in update_data.items():
        setattr(user, field, value)

    db.commit()
    db.refresh(user)
                                                                
    invalidate_user_cache(user.id)
    return user
