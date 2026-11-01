from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.crowd_log import CrowdLog
from app.models.crowd_log_hourly import CrowdLogHourly
from app.models.sensor_feed_event import SensorFeedEvent
from app.services import peak_hour_service
from app.utils.db_batch import batched_delete

logger = logging.getLogger(__name__)

def _upsert_hourly_batch(db: Session, rows) -> None:
    stmt = pg_insert(CrowdLogHourly).values(
        [
            {
                "station_id": row.station_id,
                "hour_bucket": row.hour_bucket,
                "avg_count": float(row.avg_count),
                "max_count": row.max_count,
                "min_count": row.min_count,
                "sample_count": row.sample_count,
            }
            for row in rows
        ]
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[CrowdLogHourly.station_id, CrowdLogHourly.hour_bucket],
        set_={

            "avg_count": stmt.excluded.avg_count,
            "max_count": stmt.excluded.max_count,
            "min_count": stmt.excluded.min_count,
            "sample_count": stmt.excluded.sample_count,
        },
    )
    db.execute(stmt)

def _rollup_and_delete(db: Session) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.CROWD_LOG_ROLLUP_AFTER_DAYS)

    hour_bucket = func.date_trunc("hour", CrowdLog.created_at)
    base_query = (
        db.query(
            CrowdLog.station_id,
            hour_bucket.label("hour_bucket"),
            func.avg(CrowdLog.current_count).label("avg_count"),
            func.max(CrowdLog.current_count).label("max_count"),
            func.min(CrowdLog.current_count).label("min_count"),
            func.count(CrowdLog.id).label("sample_count"),
        )
        .filter(CrowdLog.created_at < cutoff)
        .group_by(CrowdLog.station_id, hour_bucket)
        .order_by(CrowdLog.station_id, hour_bucket)
    )

    rolled_up_buckets = 0
    offset = 0
    batch_size = settings.CROWD_ROLLUP_BATCH_SIZE
    while True:
        page = base_query.limit(batch_size).offset(offset).all()
        if not page:
            break
        _upsert_hourly_batch(db, page)
        db.commit()
        rolled_up_buckets += len(page)
        if len(page) < batch_size:
            break
        offset += batch_size

    raw_rows_deleted = batched_delete(
        db,
        CrowdLog,
        CrowdLog.id,
        CrowdLog.created_at < cutoff,
        batch_size=settings.RETENTION_BATCH_SIZE,
    )
    return {"rolled_up_buckets": rolled_up_buckets, "raw_rows_deleted": raw_rows_deleted}

def _hard_delete_stale_raw(db: Session) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.CROWD_LOG_RETENTION_DAYS)
    return batched_delete(
        db, CrowdLog, CrowdLog.id, CrowdLog.created_at < cutoff,
        batch_size=settings.RETENTION_BATCH_SIZE,
    )

def _hard_delete_stale_hourly(db: Session) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.CROWD_LOG_HOURLY_RETENTION_DAYS)
    return batched_delete(
        db, CrowdLogHourly, CrowdLogHourly.id, CrowdLogHourly.hour_bucket < cutoff,
        batch_size=settings.RETENTION_BATCH_SIZE,
    )

def _hard_delete_stale_feed_events(db: Session) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.SENSOR_FEED_EVENT_RETENTION_DAYS)
    return batched_delete(
        db, SensorFeedEvent, SensorFeedEvent.id, SensorFeedEvent.created_at < cutoff,
        batch_size=settings.RETENTION_BATCH_SIZE,
    )

def run_retention_once(db: Session) -> dict:
    rollup_stats = _rollup_and_delete(db)
    raw_deleted = _hard_delete_stale_raw(db)
    hourly_deleted = _hard_delete_stale_hourly(db)
    feed_events_deleted = _hard_delete_stale_feed_events(db)
    peak_hour_stats = peak_hour_service.compute_and_cache_peak_hours(db)
    stats = {
        **rollup_stats,
        "raw_safety_net_deleted": raw_deleted,
        "hourly_rollups_deleted": hourly_deleted,
        "sensor_feed_events_deleted": feed_events_deleted,
        "peak_hours": peak_hour_stats,
    }
    if stats["rolled_up_buckets"] or stats["raw_rows_deleted"] or raw_deleted or hourly_deleted or feed_events_deleted:
        logger.info("[crowd_retention] %s", stats)
    return stats

async def run_forever(session_factory, interval_seconds: int | None = None) -> None:
    interval = interval_seconds or settings.CROWD_RETENTION_INTERVAL_SECONDS
    while True:
        db = session_factory()
        try:
            await asyncio.to_thread(run_retention_once, db)
        except Exception as exc:                
            logger.error("[crowd_retention] pass failed, will retry next interval: %s", exc, exc_info=exc)

            db.rollback()
        finally:
            db.close()
        await asyncio.sleep(interval)
