from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.core.config import settings

BUSINESS_TZ = ZoneInfo(settings.BUSINESS_TIMEZONE)

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def to_business_time(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BUSINESS_TZ)

def business_now() -> datetime:
    return to_business_time(utc_now())

def business_today() -> date:
    return business_now().date()
