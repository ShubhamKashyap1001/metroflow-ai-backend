import logging
from datetime import datetime, timezone

from app.core import notification_executor
from app.core.config import settings
from app.database.session import SessionLocal
from app.enums.notification_dispatch_kind import NotificationDispatchKind
from app.enums.notification_dispatch_status import NotificationDispatchStatus
from app.models.notification_dispatch_job import NotificationDispatchJob
from app.services import alert_service

logger = logging.getLogger(__name__)

def enqueue_and_submit(
    kind: NotificationDispatchKind,
    alert_id: int,
    actor_id: str | None,
    notify_email: bool,
    notify_sms: bool,
) -> None:
    db = SessionLocal()
    try:
        job = NotificationDispatchJob(
            kind=kind,
            alert_id=alert_id,
            actor_id=str(actor_id) if actor_id else None,
            notify_email=notify_email,
            notify_sms=notify_sms,
            status=NotificationDispatchStatus.QUEUED,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    notification_executor.submit(run_job, job_id)

def _mark(job_id: int, status: NotificationDispatchStatus, error: str | None = None) -> None:
    db = SessionLocal()
    try:
        job = db.get(NotificationDispatchJob, job_id)
        if job is None:
            return
        job.status = status
        job.last_error = error
        if status in (NotificationDispatchStatus.DONE, NotificationDispatchStatus.FAILED):
            job.completed_at = datetime.now(timezone.utc)
        db.add(job)
        db.commit()
    finally:
        db.close()

def run_job(job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.get(NotificationDispatchJob, job_id)
        if job is None:
            return
        if job.status == NotificationDispatchStatus.DONE:

            return
        job.status = NotificationDispatchStatus.IN_PROGRESS
        job.attempts += 1
        db.add(job)
        db.commit()
        kind = job.kind
        alert_id = job.alert_id
        actor_id = job.actor_id
        notify_email = job.notify_email
        notify_sms = job.notify_sms
    finally:
        db.close()

    try:
        if kind == NotificationDispatchKind.ALERT_CREATED:
            alert_service.dispatch_alert_notifications(
                alert_id, actor_id, notify_email, notify_sms, job_id=job_id
            )
        else:
            alert_service.dispatch_alert_resolution_notifications(
                alert_id, actor_id, job_id=job_id
            )
    except Exception as exc:
        _mark(job_id, NotificationDispatchStatus.FAILED, error=str(exc)[:500])
        raise
    else:
        _mark(job_id, NotificationDispatchStatus.DONE)

def recover_pending_jobs() -> int:
    db = SessionLocal()
    try:
        stuck = (
            db.query(NotificationDispatchJob)
            .filter(
                NotificationDispatchJob.status.in_(
                    [NotificationDispatchStatus.QUEUED, NotificationDispatchStatus.IN_PROGRESS]
                )
            )
            .all()
        )
        resumable_ids = []
        for job in stuck:
            if job.attempts >= settings.NOTIFICATION_DISPATCH_MAX_JOB_ATTEMPTS:
                job.status = NotificationDispatchStatus.FAILED
                job.last_error = (
                    f"Exceeded {settings.NOTIFICATION_DISPATCH_MAX_JOB_ATTEMPTS} attempts "
                    "across process restarts - giving up."
                )
                job.completed_at = datetime.now(timezone.utc)
                db.add(job)
                continue
            resumable_ids.append(job.id)
        db.commit()
    finally:
        db.close()

    if resumable_ids:

        _MAX_IDS_LOGGED = 20
        shown = resumable_ids[:_MAX_IDS_LOGGED]
        suffix = (
            f" (+{len(resumable_ids) - _MAX_IDS_LOGGED} more)"
            if len(resumable_ids) > _MAX_IDS_LOGGED
            else ""
        )
        logger.warning(
            "Resuming %d notification dispatch job(s) left queued/in-progress "
            "by a previous process (restart recovery): %s%s",
            len(resumable_ids),
            shown,
            suffix,
        )

    for job_id in resumable_ids:
        notification_executor.submit(run_job, job_id)

    return len(resumable_ids)
