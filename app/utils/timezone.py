from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.core.config import settings

BUSINESS_TZ = ZoneInfo(settings.BUSINESS_TIMEZONE)


def utc_now() -> datetime:
    """Aware current instant in UTC - unchanged persistence policy,
    kept here so callers doing business-timezone math don't need a
    separate `from datetime import datetime, timezone` import just
    for this one call."""
    return datetime.now(timezone.utc)


def to_business_time(dt: datetime) -> datetime:
    """Convert an aware (or, defensively, naive-treated-as-UTC)
    datetime into the app's configured business timezone. Use this
    before reading `.hour`, `.weekday()`, `.time()`, or `.date()` off
    a UTC instant for any date/peak-hour/schedule calculation."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BUSINESS_TZ)


def business_now() -> datetime:
    """Current instant, expressed in the app's business timezone."""
    return to_business_time(utc_now())


def business_today() -> date:
    """Today's calendar date in the app's business timezone (replaces
    the naive, server-local-clock `date.today()`)."""
    return business_now().date()
