
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.core.email import send_alert_emails
from app.core.sms import send_alert_sms
from app.database.session import SessionLocal
from app.enums.notification_channel import NotificationChannel
from app.enums.notification_source import NotificationSource
from app.enums.notification_status import NotificationStatus
from app.models.alert import Alert
from app.models.notification_log import NotificationLog
from app.models.station import Station
from app.models.user_profile import UserProfile
from app.schemas.alert import AlertCreate
from app.services import notification_service
from app.simulator.constants import SIMULATED_EMAIL_DOMAIN
from app.utils.geo import cities_for_state
from app.websocket.events import STATION_ALERT
from app.websocket.manager import manager

# Phase 9: both list_alerts (dashboard alert feed) and
# list_alert_notifications (per-alert email/SMS delivery log) used to
# run `.all()` with no limit/offset at all - on a long-running deployment
# either can grow into the tens/hundreds of thousands of rows (every
# alert ever raised; every recipient x channel row for a single alert
# sent to the whole active user base), so an unauthenticated-looking but
# otherwise ordinary GET could pull the entire table into memory and
# serialize it as one giant JSON response. Same shape as the
# MAX_BULK_RECOMMENDATION_STATIONS cap in app/api/v1/prediction.py and
# the admin /logs limit clamp - a default page size plus a hard upper
# bound, enforced here (not just in the router) so any other caller of
# these service functions gets the same protection for free.
DEFAULT_ALERTS_LIMIT = 100
MAX_ALERTS_LIMIT = 500

DEFAULT_ALERT_NOTIFICATIONS_LIMIT = 200
MAX_ALERT_NOTIFICATIONS_LIMIT = 1000

def _clamp(value: int, default: int, maximum: int) -> int:
    if value is None:
        value = default
    return min(max(value, 1), maximum)

def _broadcast_alert(db: Session, alert: Alert, resolved: bool) -> None:
    """Pushes the alert to every connected operator immediately over
    /ws/monitor - the sync/thread-safe notify() variant, since this is
    called from plain `def` routes/services running in FastAPI's
    threadpool (see app/websocket/manager.py)."""
    station = db.get(Station, alert.station_id)
    manager.notify(STATION_ALERT, {
        "alert_id": alert.id,
        "station_id": alert.station_id,
        "station_name": station.station_name if station else None,
        "alert_type": alert.alert_type.value,
        "message": alert.message,
        "available_until": alert.available_until.isoformat() if alert.available_until else None,
        "is_resolved": resolved,
        "created_at": alert.created_at.isoformat() if alert.created_at else None,
    })

def list_alerts(
    db: Session,
    station_id: int | None = None,
    active_only: bool = False,
    state: str | None = None,
    limit: int = DEFAULT_ALERTS_LIMIT,
    offset: int = 0,
) -> list[Alert]:
    limit = _clamp(limit, DEFAULT_ALERTS_LIMIT, MAX_ALERTS_LIMIT)
    offset = max(offset or 0, 0)

    query = db.query(Alert)
    if station_id:
        query = query.filter(Alert.station_id == station_id)
    if active_only:
        query = query.filter(Alert.is_resolved.is_(False))
    cities = cities_for_state(state)
    if cities:
        query = query.join(Station, Station.id == Alert.station_id).filter(
            Station.city.in_(cities)
        )
    return (
        query.order_by(Alert.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

def create_alert(db: Session, payload: AlertCreate, created_by: str | None = None) -> Alert:
                                                                     
    alert = Alert(**payload.model_dump(), created_by=created_by)
    db.add(alert)
    db.commit()
    db.refresh(alert)

    _broadcast_alert(db, alert, resolved=False)

    station = db.get(Station, alert.station_id)
    station_name = station.station_name if station else f"Station #{alert.station_id}"
    notification_service.create_notification(
        db,
        source=NotificationSource.OPERATOR,
        title=f"{alert.alert_type.value.title()} alert - {station_name}",
        message=alert.message,
        related_alert_id=alert.id,
        state=station.city if station else None,
    )

    return alert

def get_alert(db: Session, alert_id: int) -> Alert:
    alert = db.get(Alert, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    return alert

def resolve_alert(db: Session, alert_id: int) -> tuple[Alert, bool]:
    """Returns `(alert, just_resolved)`. `just_resolved` is True only
    on the call that actually flips `is_resolved` False -> True - a
    repeat call for an already-resolved alert (a retried request after
    a dropped response, a double-click, a client that times out and
    resubmits) returns the same alert with `just_resolved=False` and
    performs no further writes or broadcasts.

    The caller (PATCH /alerts/{id}/resolve) MUST gate its
    resolution-notification dispatch on `just_resolved`, not on the
    request merely having `notify_on_resolve=true` - otherwise every
    repeat/retried call re-sends the resolution email/SMS/bell
    notification for an alert that was already resolved, which is
    exactly the "duplicate notifications" bug this guards against. See
    docs/notification-delivery.md.

    The False->True flip itself is done as a single atomic
    UPDATE ... WHERE is_resolved = false (a compare-and-swap on the
    row), not a Python-level read-then-write. Two simultaneous resolve
    requests for the same alert both reach this UPDATE; the database's
    row lock lets only one of them actually match the
    `is_resolved = false` predicate and flip the row - the other
    matches zero rows and gets `just_resolved=False` back, so only one
    caller ever proceeds to broadcast/notify, regardless of request
    timing.
    """
    alert = get_alert(db, alert_id)

    resolved_at = datetime.now(timezone.utc)
    result = db.execute(
        update(Alert)
        .where(Alert.id == alert_id, Alert.is_resolved.is_(False))
        .values(is_resolved=True, resolved_at=resolved_at)
    )
    just_resolved = result.rowcount == 1

    if not just_resolved:
        # Nothing to commit or broadcast - a retried/duplicated request
        # for an already-resolved alert must be a true no-op, exactly
        # as documented above.
        return alert, False

    # Reflect the flip on the in-memory object immediately rather than
    # relying solely on db.refresh() to re-fetch it - the caller must
    # see just_resolved=True paired with an alert whose is_resolved is
    # already True.
    alert.is_resolved = True
    alert.resolved_at = resolved_at

    db.commit()
    db.refresh(alert)
    _broadcast_alert(db, alert, resolved=True)
    return alert, just_resolved

def _already_sent_recipients(
    db: Session, job_id: int | None, channel: NotificationChannel
) -> set[str]:
    """Recipients this specific dispatch job has already sent `channel`
    to, per NotificationLog. Returns an empty set when `job_id` is
    None (no idempotency scoping available - e.g. a caller outside the
    durable dispatch queue), so behaviour for such callers is
    unchanged."""
    if job_id is None:
        return set()
    rows = (
        db.query(NotificationLog.recipient)
        .filter(
            NotificationLog.job_id == job_id,
            NotificationLog.channel == channel,
            NotificationLog.status == NotificationStatus.SENT,
        )
        .all()
    )
    return {row[0] for row in rows}

def _log_results(
    db: Session,
    alert_id: int,
    channel: NotificationChannel,
    results: dict[str, str],
    station_city: str | None = None,
    job_id: int | None = None,
) -> None:
    for recipient, outcome in results.items():
        is_sent = outcome == "sent"
        db.add(
            NotificationLog(
                alert_id=alert_id,
                job_id=job_id,
                channel=channel,
                recipient=recipient,
                status=NotificationStatus.SENT if is_sent else NotificationStatus.FAILED,
                error_message=None if is_sent else outcome,
                sent_at=datetime.now(timezone.utc) if is_sent else None,
            )
        )
    db.commit()

    if channel == NotificationChannel.EMAIL:
        sent_count = sum(1 for outcome in results.values() if outcome == "sent")
        if sent_count:
            notification_service.create_notification(
                db,
                source=NotificationSource.EMAIL,
                title="Email notifications sent",
                message=f"Email notification sent to {sent_count} recipient(s) for alert #{alert_id}.",
                related_alert_id=alert_id,
                state=station_city,
            )

def _dispatch(
    alert_id: int,
    created_by_id: str | None,
    notify_email: bool,
    notify_sms: bool,
    resolved: bool,
    job_id: int | None = None,
) -> None:
    """Runs in a FastAPI BackgroundTask (its own thread, its own DB
    session - never the request's).

    Phase 6 fix: this used to hold ONE db session/connection open for
    its entire body, including the two blocking network calls
    (send_alert_emails / send_alert_sms) - a real "long transaction"
    bug: a slow SMTP/Twilio round trip (or many recipients) held a
    pooled connection idle-but-checked-out for the whole time, making
    it unavailable to every other request. Restructured into three
    short, independent steps so a DB connection is only ever held for
    the fast read/write portions - see docs/database-sessions-and-connection-pooling.md.

    BUGFIX (duplicate dispatch on crash-recovery): `job_id` scopes this
    call to a single notification_dispatch_jobs row. When
    notification_dispatch_queue.run_job resumes a job that a previous
    process died in the middle of, some recipients may already have a
    logged SENT NotificationLog row for this exact job_id from that
    earlier, incomplete attempt (the emails/SMS genuinely went out
    before the crash - only the bookkeeping after didn't finish). Those
    recipients are excluded before the network call is ever made, so a
    resumed job cannot re-send to someone it already successfully
    reached. Recipients that were never attempted, or that failed, are
    NOT excluded - retries are unaffected. `job_id=None` (any caller
    outside the durable queue) disables this filtering entirely,
    matching prior behaviour."""
    if not notify_email and not notify_sms:
        return

    # Step 1: read everything this dispatch needs, as plain values (not
    # ORM objects), then close the session immediately - nothing below
    # this block touches `db1`.
    db1 = SessionLocal()
    try:
        alert = db1.get(Alert, alert_id)
        if not alert:
            return

        station = db1.get(Station, alert.station_id)
        station_name = station.station_name if station else f"Station #{alert.station_id}"
        station_city = station.city if station else None
        available_until = (
            alert.available_until.isoformat() if alert.available_until else None
        )
        alert_type = alert.alert_type.value
        alert_message = alert.message
        alert_created_at = alert.created_at.isoformat()

        # RAM FIX (Render Free 512MB): this used to pull full UserProfile
        # ORM rows (id, email, full_name, username, phone, avatar_url,
        # role, is_active, timestamps) for every active real user just
        # to read two columns off each one below - on a deployment with
        # a large passenger base this is real per-broadcast memory that
        # scales with the whole user table, not with anything about the
        # alert itself, and every alert create/resolve re-runs it.
        # Selecting only (email, phone) keeps the exact same recipient
        # set (same filter, same rows) while avoiding hydrating a full
        # ORM instance - and every column that's still an inflated
        # Python object either way - per active user.
        active_user_rows = (
            db1.query(UserProfile.email, UserProfile.phone)
            .filter(
                UserProfile.is_active.is_(True),
                or_(
                    UserProfile.email.is_(None),
                    ~UserProfile.email.like(f"%@{SIMULATED_EMAIL_DOMAIN}"),
                ),
            )
            .all()
        )
        creator = db1.get(UserProfile, created_by_id) if created_by_id else None

        emails = {email for email, _phone in active_user_rows if email}
        if creator and creator.email:
            emails.add(creator.email)
        phones = {phone for _email, phone in active_user_rows if phone}
        if creator and creator.phone:
            phones.add(creator.phone)

        # Idempotency: drop anyone this exact job already succeeded in
        # sending to on a previous (crashed/resumed) attempt, so a
        # re-run can only ever reach a given recipient once.
        if job_id is not None:
            emails -= _already_sent_recipients(db1, job_id, NotificationChannel.EMAIL)
            phones -= _already_sent_recipients(db1, job_id, NotificationChannel.SMS)
    finally:
        db1.close()

    # Step 2: the actual slow part - blocking SMTP/Twilio network calls,
    # deliberately done with NO db session open at all.
    email_results = None
    sms_results = None
    if notify_email and emails:
        email_results = send_alert_emails(
            recipients=list(emails),
            station_name=station_name,
            alert_type=alert_type,
            message=alert_message,
            created_at=alert_created_at,
            available_until=available_until,
            resolved=resolved,
        )
    if notify_sms and phones:
        sms_results = send_alert_sms(
            recipients=list(phones),
            station_name=station_name,
            alert_type=alert_type,
            message=alert_message,
            available_until=available_until,
            resolved=resolved,
        )

    # Step 3: a second short session just to log the outcomes - opened
    # only now that the slow network calls are already done.
    if email_results is None and sms_results is None:
        return
    db2 = SessionLocal()
    try:
        if email_results is not None:
            _log_results(
                db2, alert_id, NotificationChannel.EMAIL, email_results,
                station_city=station_city, job_id=job_id,
            )
        if sms_results is not None:
            _log_results(
                db2, alert_id, NotificationChannel.SMS, sms_results,
                station_city=station_city, job_id=job_id,
            )
    except Exception:
        db2.rollback()
        raise
    finally:
        db2.close()

def dispatch_alert_notifications(
    alert_id: int,
    created_by_id: str | None,
    notify_email: bool,
    notify_sms: bool,
    job_id: int | None = None,
) -> None:
    """Send the original alert email and/or SMS to every active user,
    plus an explicit copy to whoever raised it, and log one
    NotificationLog row per (channel, recipient).

    `job_id` (the owning notification_dispatch_jobs row, when called
    via the durable dispatch queue) scopes the crash-recovery
    idempotency check in `_dispatch` - see its docstring."""
    _dispatch(alert_id, created_by_id, notify_email, notify_sms, resolved=False, job_id=job_id)

def dispatch_alert_resolution_notifications(
    alert_id: int, resolved_by_id: str | None, job_id: int | None = None
) -> None:
    """Re-notify the same audience that the alert has been resolved,
    on the same channel(s) (email/SMS) it was originally raised on -
    read from the alert's own notify_email/notify_sms columns, so the
    caller (the /resolve endpoint) doesn't need to repeat them.

    `job_id` (the owning notification_dispatch_jobs row, when called
    via the durable dispatch queue) scopes the crash-recovery
    idempotency check in `_dispatch` - see its docstring."""
    db = SessionLocal()
    try:
        alert = db.get(Alert, alert_id)
        if not alert:
            return
        notify_email = alert.notify_email
        notify_sms = alert.notify_sms
    finally:
        db.close()

    _dispatch(alert_id, resolved_by_id, notify_email, notify_sms, resolved=True, job_id=job_id)

def list_alert_notifications(
    db: Session,
    alert_id: int,
    limit: int = DEFAULT_ALERT_NOTIFICATIONS_LIMIT,
    offset: int = 0,
) -> list[NotificationLog]:
    limit = _clamp(limit, DEFAULT_ALERT_NOTIFICATIONS_LIMIT, MAX_ALERT_NOTIFICATIONS_LIMIT)
    offset = max(offset or 0, 0)

    return (
        db.query(NotificationLog)
        .filter(NotificationLog.alert_id == alert_id)
        .order_by(NotificationLog.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
