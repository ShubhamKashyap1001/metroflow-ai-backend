
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
from app.models.alert_incident_event import AlertIncidentEvent
from app.models.notification_log import NotificationLog
from app.models.station import Station
from app.models.user_profile import UserProfile
from app.schemas.alert import AlertCreate
from app.services import notification_service
from app.simulator.constants import SIMULATED_EMAIL_DOMAIN
from app.utils.geo import cities_for_state
from app.websocket.events import STATION_ALERT
from app.websocket.manager import manager

DEFAULT_ALERTS_LIMIT = 100
MAX_ALERTS_LIMIT = 500

DEFAULT_ALERT_NOTIFICATIONS_LIMIT = 200
MAX_ALERT_NOTIFICATIONS_LIMIT = 1000

def _clamp(value: int, default: int, maximum: int) -> int:
    if value is None:
        value = default
    return min(max(value, 1), maximum)

def _broadcast_alert(db: Session, alert: Alert, resolved: bool) -> None:
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
    db.add(AlertIncidentEvent(alert_id=alert.id, actor_id=created_by, action="created"))
    db.commit()

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

def resolve_alert(db: Session, alert_id: int, actor_id=None) -> tuple[Alert, bool]:
    alert = get_alert(db, alert_id)

    resolved_at = datetime.now(timezone.utc)
    result = db.execute(
        update(Alert)
        .where(Alert.id == alert_id, Alert.is_resolved.is_(False))
        .values(is_resolved=True, resolved_at=resolved_at)
    )
    just_resolved = result.rowcount == 1

    if not just_resolved:

        return alert, False

    alert.is_resolved = True
    alert.resolved_at = resolved_at

    db.add(AlertIncidentEvent(alert_id=alert.id, actor_id=actor_id, action="resolved"))
    db.commit()
    db.refresh(alert)
    _broadcast_alert(db, alert, resolved=True)
    return alert, just_resolved

def acknowledge_alert(db: Session, alert_id: int, actor_id, note: str | None = None) -> Alert:
    alert = get_alert(db, alert_id)
    if alert.is_resolved:
        raise HTTPException(status_code=409, detail="Resolved alerts cannot be acknowledged")
    if alert.acknowledged_at is None:
        alert.acknowledged_at = datetime.now(timezone.utc)
        alert.acknowledged_by_id = actor_id
        db.add(AlertIncidentEvent(alert_id=alert.id, actor_id=actor_id, action="acknowledged", note=(note or "").strip() or None))
        db.commit()
        db.refresh(alert)
        _broadcast_alert(db, alert, resolved=False)
    return alert

def assign_alert(db: Session, alert_id: int, assigned_to_id, actor_id) -> Alert:
    alert = get_alert(db, alert_id)
    if alert.is_resolved:
        raise HTTPException(status_code=409, detail="Resolved alerts cannot be reassigned")
    assignee = db.get(UserProfile, assigned_to_id) if assigned_to_id else None
    if assigned_to_id and (not assignee or not assignee.is_active or assignee.role.value not in ("admin", "operator")):
        raise HTTPException(status_code=422, detail="Assignee must be an active admin or operator")
    if alert.assigned_to_id != assigned_to_id:
        alert.assigned_to_id = assigned_to_id
        db.add(AlertIncidentEvent(
            alert_id=alert.id,
            actor_id=actor_id,
            action="unassigned" if assigned_to_id is None else "assigned",
            note=f"Assigned to {assignee.full_name}" if assignee else "Assignment cleared",
        ))
        db.commit()
        db.refresh(alert)
        _broadcast_alert(db, alert, resolved=False)
    return alert

def list_incident_events(db: Session, alert_id: int) -> list[AlertIncidentEvent]:
    get_alert(db, alert_id)
    return (
        db.query(AlertIncidentEvent)
        .filter(AlertIncidentEvent.alert_id == alert_id)
        .order_by(AlertIncidentEvent.created_at.desc(), AlertIncidentEvent.id.desc())
        .limit(100)
        .all()
    )

def _already_sent_recipients(
    db: Session, job_id: int | None, channel: NotificationChannel
) -> set[str]:
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
    if not notify_email and not notify_sms:
        return

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

        if job_id is not None:
            emails -= _already_sent_recipients(db1, job_id, NotificationChannel.EMAIL)
            phones -= _already_sent_recipients(db1, job_id, NotificationChannel.SMS)
    finally:
        db1.close()

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
    _dispatch(alert_id, created_by_id, notify_email, notify_sms, resolved=False, job_id=job_id)

def dispatch_alert_resolution_notifications(
    alert_id: int, resolved_by_id: str | None, job_id: int | None = None
) -> None:
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
