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
from app.utils.db_batch import batched_delete

logger = logging.getLogger(__name__)


def _upsert_hourly_batch(db: Session, rows) -> None:
    """Bulk-upsert one page of (station, hour) aggregate rows in a
    single multi-row INSERT ... ON CONFLICT statement - one round trip
    per batch instead of one INSERT per bucket."""
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
            # A rollup can run more than once against an overlapping
            # window (e.g. the job was delayed and two runs both see
            # the same old hour) - recomputing from scratch instead of
            # accumulating avoids double-counting samples on a second
            # pass.
            "avg_count": stmt.excluded.avg_count,
            "max_count": stmt.excluded.max_count,
            "min_count": stmt.excluded.min_count,
            "sample_count": stmt.excluded.sample_count,
        },
    )
    db.execute(stmt)


def _rollup_and_delete(db: Session) -> dict:
    """Aggregate raw crowd_logs rows older than
    CROWD_LOG_ROLLUP_AFTER_DAYS into crowd_logs_hourly, then delete
    them. Returns counters for observability/logging.

    Both steps are paginated/batched instead of a single unbounded
    pass over the whole backlog:

      - The aggregate SELECT used to run once with a bare `.all()`,
        pulling every (station, hour) bucket the backlog produced into
        one Python list before looping over it and issuing one INSERT
        per bucket. A job that's fallen behind (disabled for a while,
        or a bulk historical import) can turn that into tens of
        thousands of buckets materialized at once just to loop over
        them one at a time. It's now paginated at
        CROWD_ROLLUP_BATCH_SIZE buckets per page, each page upserted
        in a single multi-row INSERT and committed before the next
        page is fetched - memory stays bounded to one page, and no
        single transaction covers the whole backlog.
      - The raw-row delete for the same cutoff goes through
        batched_delete (see app/utils/db_batch.py) for the same
        reason: it used to be one unbounded
        `query.filter(...).delete()` that could hold locks against
        the entire backlog for as long as it took to remove it.
    """
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
    """Safety-net delete: raw crowd_logs older than
    CROWD_LOG_RETENTION_DAYS never survives regardless of whether the
    rollup step above ran successfully for it. Batched (see
    app/utils/db_batch.py) for the same memory/long-transaction
    reasons as the rollup step's delete."""
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


def run_retention_once(db: Session) -> dict:
    """One full retention pass: rollup+delete, then both hard-delete
    safety nets. Synchronous - callers running on the event loop
    should wrap this in asyncio.to_thread (see run_forever below)."""
    rollup_stats = _rollup_and_delete(db)
    raw_deleted = _hard_delete_stale_raw(db)
    hourly_deleted = _hard_delete_stale_hourly(db)
    stats = {
        **rollup_stats,
        "raw_safety_net_deleted": raw_deleted,
        "hourly_rollups_deleted": hourly_deleted,
    }
    if stats["rolled_up_buckets"] or stats["raw_rows_deleted"] or raw_deleted or hourly_deleted:
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
            # Phase 6: explicit rollback before close - see
            # csv_replay_simulator.py's run_forever for why. Especially
            # relevant here since one pass does three separate
            # query+commit steps on the same session - a failure in
            # step 2 or 3 must not leave anything from that step
            # half-applied when the session goes back to the pool.
            db.rollback()
        finally:
            db.close()
        await asyncio.sleep(interval)
