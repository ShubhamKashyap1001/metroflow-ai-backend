"""News & Announcements module. Admins/operators publish "latest
news" items (service updates, general announcements); every
authenticated user (passenger included) reads the published feed.
Separate from the Alert module (station-scoped, email/SMS-dispatched
incident alerts) - this is lightweight, unscoped announcements.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.security import get_current_user, require_roles
from app.database.session import get_db
from app.enums.user_role import UserRole
from app.models.user_profile import UserProfile
from app.schemas.news import NewsCreate, NewsResponse, NewsUpdate
from app.services import news_service

router = APIRouter(
    prefix="/news",
    tags=["News"]
)

@router.get("/", response_model=list[NewsResponse])
def get_news_feed(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    if include_inactive and current_user.role not in (UserRole.ADMIN, UserRole.OPERATOR):
        include_inactive = False
    return news_service.list_news(db, include_inactive)

@router.post("/", response_model=NewsResponse, status_code=201)
def create_news(
    payload: NewsCreate,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return news_service.create_news(db, payload, created_by=current_user.id)

@router.get("/{news_id}", response_model=NewsResponse)
def get_news_item(
    news_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return news_service.get_news(db, news_id)

@router.patch("/{news_id}", response_model=NewsResponse)
def update_news(
    news_id: int,
    payload: NewsUpdate,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return news_service.update_news(db, news_id, payload)

@router.delete("/{news_id}", status_code=204)
def delete_news(
    news_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    news_service.delete_news(db, news_id)
