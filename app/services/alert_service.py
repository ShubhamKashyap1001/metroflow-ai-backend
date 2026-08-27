
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import or_
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
) -> list[Alert]:
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
    return query.order_by(Alert.created_at.desc()).all()

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

def resolve_alert(db: Session, alert_id: int) -> Alert:
    alert = get_alert(db, alert_id)
    if not alert.is_resolved:
        alert.is_resolved = True
        alert.resolved_at = datetime.now(timezone.utc)
        db.add(alert)
        db.commit()
        db.refresh(alert)
        _broadcast_alert(db, alert, resolved=True)
    return alert

def _log_results(db: Session, alert_id: int, channel: NotificationChannel, results: dict[str, str]) -> None:
    for recipient, outcome in results.items():
        is_sent = outcome == "sent"
        db.add(
            NotificationLog(
                alert_id=alert_id,
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
            alert = db.get(Alert, alert_id)
            station = db.get(Station, alert.station_id) if alert else None
            notification_service.create_notification(
                db,
                source=NotificationSource.EMAIL,
                title="Email notifications sent",
                message=f"Email notification sent to {sent_count} recipient(s) for alert #{alert_id}.",
                related_alert_id=alert_id,
                state=station.city if station else None,
            )

def _dispatch(
    alert_id: int,
    created_by_id: str | None,
    notify_email: bool,
    notify_sms: bool,
    resolved: bool,
) -> None:

    if not notify_email and not notify_sms:
        return

    db = SessionLocal()
    try:
        alert = db.get(Alert, alert_id)
        if not alert:
            return

        station = db.get(Station, alert.station_id)
        station_name = station.station_name if station else f"Station #{alert.station_id}"

        available_until = (
            alert.available_until.isoformat() if alert.available_until else None
        )

        active_users = (
            db.query(UserProfile)
            .filter(
                UserProfile.is_active.is_(True),
                or_(
                    UserProfile.email.is_(None),
                    ~UserProfile.email.like(f"%@{SIMULATED_EMAIL_DOMAIN}"),
                ),
            )
            .all()
        )

        creator = db.get(UserProfile, created_by_id) if created_by_id else None

        if notify_email:
            emails = {u.email for u in active_users if u.email}
            if creator and creator.email:
                emails.add(creator.email)
            if emails:
                results = send_alert_emails(
                    recipients=list(emails),
                    station_name=station_name,
                    alert_type=alert.alert_type.value,
                    message=alert.message,
                    created_at=alert.created_at.isoformat(),
                    available_until=available_until,
                    resolved=resolved,
                )
                _log_results(db, alert.id, NotificationChannel.EMAIL, results)

        if notify_sms:
                                                                     
            phones = {u.phone for u in active_users if u.phone}
            if creator and creator.phone:
                phones.add(creator.phone)
            if phones:
                sms_results = send_alert_sms(
                    recipients=list(phones),
                    station_name=station_name,
                    alert_type=alert.alert_type.value,
                    message=alert.message,
                    available_until=available_until,
                    resolved=resolved,
                )
                _log_results(db, alert.id, NotificationChannel.SMS, sms_results)
    finally:
        db.close()

def dispatch_alert_notifications(
    alert_id: int,
    created_by_id: str | None,
    notify_email: bool,
    notify_sms: bool,
) -> None:
    """Send the original alert email and/or SMS to every active user,
    plus an explicit copy to whoever raised it, and log one
    NotificationLog row per (channel, recipient)."""
    _dispatch(alert_id, created_by_id, notify_email, notify_sms, resolved=False)

def dispatch_alert_resolution_notifications(alert_id: int, resolved_by_id: str | None) -> None:
    """Re-notify the same audience that the alert has been resolved,
    on the same channel(s) (email/SMS) it was originally raised on -
    read from the alert's own notify_email/notify_sms columns, so the
    caller (the /resolve endpoint) doesn't need to repeat them."""
    db = SessionLocal()
    try:
        alert = db.get(Alert, alert_id)
        if not alert:
            return
        notify_email = alert.notify_email
        notify_sms = alert.notify_sms
    finally:
        db.close()

    _dispatch(alert_id, resolved_by_id, notify_email, notify_sms, resolved=True)

def list_alert_notifications(db: Session, alert_id: int) -> list[NotificationLog]:
    return (
        db.query(NotificationLog)
        .filter(NotificationLog.alert_id == alert_id)
        .order_by(NotificationLog.created_at.desc())
        .all()
    )
