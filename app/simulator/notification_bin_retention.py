"""Notification Bin retention background job.

A "mark all as read" sweep (notification_service.mark_all_read) stamps
`binned_at` on every row it touches, which immediately pulls those
rows out of the Inbox feed (list_notifications) and into the Bin tab
(list_binned_notifications) - see the Bin section of
app/models/notification.py's module docstring for the full picture.

This module is the other half of that promise: every
NOTIFICATION_BIN_RETENTION_INTERVAL_SECONDS (default 5 min), hard-
delete any notification row whose `binned_at` is older than
NOTIFICATION_BIN_RETENTION_HOURS (default 72h). Unlike the 7-day Inbox
retention (enforced purely at read time, see notification_service.py's
module docstring), the Bin's 72h promise is enforced by actually
deleting the row - once something's been sitting in the Bin for a day,
it's gone for good, not just hidden.

Same run_forever/scheduler + LeaderElection pattern already used for
the crowd/train simulators and the crowd_logs retention job (see
app/simulator/retention.py and app/simulator/scheduler.py), gated by
ENABLE_NOTIFICATION_BIN_RETENTION_JOB so it can be turned off the same
way.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.notification import Notification
from app.utils.db_batch import batched_delete

logger = logging.getLogger(__name__)


def _bin_cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(
        hours=settings.NOTIFICATION_BIN_RETENTION_HOURS
    )


def run_retention_once(db: Session) -> dict:
    """One pass: hard-delete every binned notification older than
    NOTIFICATION_BIN_RETENTION_HOURS. Synchronous - callers running on
    the event loop should wrap this in asyncio.to_thread (see
    run_forever below).

    Batched (see app/utils/db_batch.py) instead of one unbounded
    `query.filter(...).delete()`: a busy broadcast day (see
    notification_service.py) can bin thousands of rows in one "mark
    all as read" sweep, and this job runs every
    NOTIFICATION_BIN_RETENTION_INTERVAL_SECONDS (5 min by default) -
    an unbatched delete would hold locks against the whole matching
    set for as long as removing it takes, on a table other requests
    (list_notifications, mark_read, mark_all_read) are actively
    reading/writing at the same time.
    """
    deleted = batched_delete(
        db,
        Notification,
        Notification.id,
        (Notification.binned_at.isnot(None)) & (Notification.binned_at < _bin_cutoff()),
        batch_size=settings.RETENTION_BATCH_SIZE,
    )
    stats = {"binned_notifications_deleted": deleted}
    if deleted:
        logger.info("[notification_bin_retention] %s", stats)
    return stats


async def run_forever(session_factory, interval_seconds: int | None = None) -> None:
    interval = interval_seconds or settings.NOTIFICATION_BIN_RETENTION_INTERVAL_SECONDS
    while True:
        db = session_factory()
        try:
            await asyncio.to_thread(run_retention_once, db)
        except Exception as exc:
            logger.error(
                "[notification_bin_retention] pass failed, will retry next interval: %s",
                exc,
                exc_info=exc,
            )
            # Explicit rollback before close - see csv_replay_simulator.
            # py's run_forever / retention.py's run_forever for why: a
            # failed delete+commit must not leave anything half-applied
            # on the session before it goes back to the pool.
            db.rollback()
        finally:
            db.close()
        await asyncio.sleep(interval)