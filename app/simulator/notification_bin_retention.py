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

            db.rollback()
        finally:
            db.close()
        await asyncio.sleep(interval)
