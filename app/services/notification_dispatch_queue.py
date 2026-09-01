"""Durable dispatch queue for alert email/SMS notifications.

See app/models/notification_dispatch_job.py for the full "why" behind
this module. Short version: `app/core/notification_executor.py`'s
`ThreadPoolExecutor` is purely in-memory, so a process restart at the
wrong moment silently drops a queued or in-flight dispatch. This
module makes the *decision to dispatch* durable (a committed DB row)
before the *work* is handed to that in-memory pool, and knows how to
find and resubmit anything a previous process didn't finish.

app/api/v1/alerts.py calls `enqueue_and_submit(...)` instead of
`notification_executor.submit(...)` directly for alert email/SMS
dispatch. app/main.py's lifespan calls `recover_pending_jobs()` once
on startup, before the app starts serving traffic.
"""
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
    """Persist a QUEUED job row (committed) BEFORE handing it to
    notification_executor's in-memory pool. If the process dies before
    `notification_executor.submit` even runs - or while the job is
    still sitting in that executor's own internal queue - the row is
    still there for `recover_pending_jobs()` to find on the next
    startup, instead of the dispatch being silently lost."""
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
    """Runs on notification_executor's worker pool (submitted from
    enqueue_and_submit, or resubmitted by recover_pending_jobs after a
    restart).

    Marks the job IN_PROGRESS - and records the attempt - BEFORE doing
    any real work, so a hard process kill mid-send leaves an
    IN_PROGRESS row (with `attempts` already reflecting this try) for
    the next startup's recovery to find, rather than a row that looks
    untouched. Then DONE or FAILED once the underlying dispatch call
    returns or raises.

    Deliberately re-runs the full dispatch (dispatch_alert_notifications
    / dispatch_alert_resolution_notifications) rather than resuming
    mid-recipient-list - see the module docstring on
    NotificationDispatchJob for why that's an acceptable "possibly
    sent twice" trade-off instead of restructuring the per-recipient
    send/log logic to be resumable."""
    db = SessionLocal()
    try:
        job = db.get(NotificationDispatchJob, job_id)
        if job is None:
            return
        if job.status == NotificationDispatchStatus.DONE:
            # Already completed by an earlier attempt (e.g. recovery
            # resubmitted it just as the original run was finishing) -
            # never re-run a job that already succeeded.
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
            alert_service.dispatch_alert_notifications(alert_id, actor_id, notify_email, notify_sms)
        else:
            alert_service.dispatch_alert_resolution_notifications(alert_id, actor_id)
    except Exception as exc:
        _mark(job_id, NotificationDispatchStatus.FAILED, error=str(exc)[:500])
        raise
    else:
        _mark(job_id, NotificationDispatchStatus.DONE)

def recover_pending_jobs() -> int:
    """Called once from app/main.py's lifespan, before the app starts
    serving traffic. Finds every job a previous process left QUEUED or
    IN_PROGRESS - this is a fresh startup, so by definition nothing is
    currently working on either state - and resubmits it, unless
    `attempts` already shows it's exhausted its
    NOTIFICATION_DISPATCH_MAX_JOB_ATTEMPTS budget across restarts (a
    job that deterministically crashes the process is marked
    permanently FAILED instead of retried forever). Returns how many
    jobs were resubmitted, for the startup log line."""
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

    for job_id in resumable_ids:
        logger.warning(
            "Resuming notification dispatch job %s left queued/in-progress by a "
            "previous process (restart recovery).",
            job_id,
        )
        notification_executor.submit(run_job, job_id)

    return len(resumable_ids)
