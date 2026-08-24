from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.enums.notification_source import NotificationSource
from app.models.news import News
from app.schemas.news import NewsCreate, NewsUpdate
from app.services import notification_service

def list_news(db: Session, include_inactive: bool = False) -> list[News]:
    """Passengers (and the public news feed) only ever see
    include_inactive=False - published news only. Admins/operators can
    pass include_inactive=True to also see drafts/unpublished items
    while managing the feed."""
    query = db.query(News)
    if not include_inactive:
        query = query.filter(News.is_active.is_(True))
    return query.order_by(News.created_at.desc()).all()

def create_news(db: Session, payload: NewsCreate, created_by) -> News:
    news = News(**payload.model_dump(), created_by=created_by)
    db.add(news)
    db.commit()
    db.refresh(news)

    notification_service.create_notification(
        db,
        source=NotificationSource.SYSTEM,
        title=news.title,
        message=news.content,
    )

    return news

def get_news(db: Session, news_id: int) -> News:
    news = db.get(News, news_id)
    if not news:
        raise HTTPException(status_code=404, detail="News item not found")
    return news

def update_news(db: Session, news_id: int, payload: NewsUpdate) -> News:
    news = get_news(db, news_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(news, field, value)
    db.add(news)
    db.commit()
    db.refresh(news)
    return news

def delete_news(db: Session, news_id: int) -> None:
    news = get_news(db, news_id)
    db.delete(news)
    db.commit()
