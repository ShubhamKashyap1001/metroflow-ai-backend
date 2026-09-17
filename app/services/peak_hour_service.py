# app/services/peak_hour_service.py
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import cache
from app.core.config import settings
from app.models.crowd_log_hourly import CrowdLogHourly
from app.models.station import Station

logger = logging.getLogger(__name__)

PEAK_HOUR_CACHE_KEY = "crowd:peak_hours"


def _compute_peak_hours(db: Session) -> list[dict]:
    """One GROUP BY over crowd_logs_hourly (station_id, hour-of-day),
    scoped to the last PEAK_HOUR_LOOKBACK_DAYS. crowd_logs_hourly is
    already bounded (~3.1M rows at the 400-day retention default - see
    docs/crowd-live-state-and-retention.md), and this only reads a
    recent slice of it, grouped (never materializing raw rows) - at 324
    stations x 24 hours that's at most ~7.8k result rows regardless of
    how much history the table eventually holds.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.PEAK_HOUR_LOOKBACK_DAYS)
    hour_of_day = func.extract("hour", CrowdLogHourly.hour_bucket)

    rows = (
        db.query(
            CrowdLogHourly.station_id,
            hour_of_day.label("hour_of_day"),
            func.avg(CrowdLogHourly.avg_count).label("avg_count"),
        )
        .filter(CrowdLogHourly.hour_bucket >= cutoff)
        .group_by(CrowdLogHourly.station_id, hour_of_day)
        .all()
    )

    # Reduce to one (peak hour, peak avg_count) pair per station - done
    # in Python since it's a tiny result set (<=7.8k rows), not worth a
    # second round trip for a window function.
    best_by_station: dict[int, tuple[int, float]] = {}
    for station_id, hour_of_day_val, avg_count in rows:
        hour_int = int(hour_of_day_val)
        current_best = best_by_station.get(station_id)
        if current_best is None or avg_count > current_best[1]:
            best_by_station[station_id] = (hour_int, float(avg_count))

    if not best_by_station:
        return []

    station_ids = list(best_by_station.keys())
    names = dict(
        db.query(Station.id, Station.station_name)
        .filter(Station.id.in_(station_ids))
        .all()
    )

    ranked = sorted(best_by_station.items(), key=lambda kv: kv[1][1], reverse=True)
    top_n = ranked[: settings.PEAK_HOUR_TOP_N]

    return [
        {
            "station_id": station_id,
            "station_name": names.get(station_id, f"Station {station_id}"),
            "peak_hour_utc": peak_hour,
            "avg_count_at_peak": round(avg_count, 1),
        }
        for station_id, (peak_hour, avg_count) in top_n
    ]


def compute_and_cache_peak_hours(db: Session) -> dict:
    """Called once per retention-job pass (hourly) - never on a
    request path, so no user traffic can trigger this query. Failure
    here must never break the retention job it rides along with, so
    it's caught and logged, not re-raised."""
    if not settings.ENABLE_PEAK_HOUR_CACHE:
        return {"skipped": True}
    try:
        stations = _compute_peak_hours(db)
        payload = {
            "stations": stations,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        cache.set_json(
            PEAK_HOUR_CACHE_KEY, payload, ttl_seconds=settings.PEAK_HOUR_CACHE_TTL_SECONDS
        )
        return {"stations_cached": len(stations)}
    except Exception as exc:
        logger.error(
            "[peak_hour_service] compute failed, will retry next retention pass: %s",
            exc,
            exc_info=exc,
        )
        return {"error": str(exc)}


def get_cached_peak_hours() -> dict:
    """Read-only, served straight from Redis - never falls back to a
    live DB query, so a client refreshing this endpoint repeatedly (or
    an empty cache right after a fresh deploy, before the first
    retention pass has run) can never generate extra DB load."""
    cached = cache.get_json(PEAK_HOUR_CACHE_KEY)
    if not cached:
        return {"stations": [], "generated_at": None}
    return cached