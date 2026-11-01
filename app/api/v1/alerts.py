from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.rate_limit import WRITE_LIMIT, limiter
from app.core.config import settings
from app.core.security import get_current_user, require_roles
from app.core import notification_executor
from app.database.session import get_db
from app.enums.alert_type import AlertType
from app.enums.notification_dispatch_kind import NotificationDispatchKind
from app.enums.user_role import UserRole
from app.models.user_profile import UserProfile
from app.models.station import Station
from app.schemas.alert import (
    AlertAcknowledge, AlertAssignment, AlertCreate, AlertIncidentEventResponse,
    AlertResolve, AlertResponse,
)
from app.schemas.notification_log import NotificationLogResponse
from app.services import alert_service
from app.services import notification_dispatch_queue
from app.services import public_address_service

router = APIRouter(
    prefix="/alerts",
    tags=["Alerts"]
)

@router.get("/", response_model=list[AlertResponse])
def get_alerts(
    station_id: int | None = None,
    active_only: bool = False,
    state: str | None = None,
    limit: int = alert_service.DEFAULT_ALERTS_LIMIT,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return alert_service.list_alerts(db, station_id, active_only, state, limit, offset)

@router.post("/", response_model=AlertResponse, status_code=201)
@limiter.limit(WRITE_LIMIT)
def create_alert(
    request: Request,
    payload: AlertCreate,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    alert = alert_service.create_alert(db, payload, created_by=current_user.id)
    station = db.get(Station, alert.station_id)
    channel_payload = {
        "event": "metro_alert",
        "alert_id": alert.id,
        "alert_type": alert.alert_type.value,
        "station_id": alert.station_id,
        "station_code": station.station_code if station else None,
        "station_name": station.station_name if station else None,
        "message": alert.message,
        "created_at": alert.created_at.isoformat() if alert.created_at else None,
    }

    if alert.alert_type == AlertType.EMERGENCY:
        notification_executor.submit(
            public_address_service.dispatch_emergency_alert,
            channel_payload,
        )
    if settings.MOBILE_PUSH_WEBHOOK_URL and settings.MOBILE_PUSH_WEBHOOK_TOKEN:
        notification_executor.submit(public_address_service.dispatch_mobile_push_alert, channel_payload)

    if payload.notify_email or payload.notify_sms:

        notification_dispatch_queue.enqueue_and_submit(
            NotificationDispatchKind.ALERT_CREATED,
            alert.id,
            current_user.id,
            payload.notify_email,
            payload.notify_sms,
        )

    return alert

@router.get("/{alert_id}", response_model=AlertResponse)
def get_alert(
    alert_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return alert_service.get_alert(db, alert_id)

@router.patch("/{alert_id}/resolve", response_model=AlertResponse)
@limiter.limit(WRITE_LIMIT)
def resolve_alert(
    request: Request,
    alert_id: int,
    payload: AlertResolve = AlertResolve(),
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    alert, just_resolved = alert_service.resolve_alert(db, alert_id, current_user.id)

    if just_resolved:
        station = db.get(Station, alert.station_id)
        resolved_payload = {
            "event": "metro_alert_resolved",
            "alert_id": alert.id,
            "alert_type": alert.alert_type.value,
            "station_id": alert.station_id,
            "station_code": station.station_code if station else None,
            "station_name": station.station_name if station else None,
            "message": alert.message,
            "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
        }
        if settings.MOBILE_PUSH_WEBHOOK_URL and settings.MOBILE_PUSH_WEBHOOK_TOKEN:
            notification_executor.submit(public_address_service.dispatch_mobile_push_alert, resolved_payload)
        if alert.alert_type == AlertType.EMERGENCY:
            notification_executor.submit(public_address_service.dispatch_emergency_alert, resolved_payload)

    if just_resolved and payload.notify_on_resolve:

        notification_dispatch_queue.enqueue_and_submit(
            NotificationDispatchKind.ALERT_RESOLVED,
            alert.id,
            current_user.id,
            notify_email=False,
            notify_sms=False,
        )

    return alert

@router.patch("/{alert_id}/acknowledge", response_model=AlertResponse)
@limiter.limit(WRITE_LIMIT)
def acknowledge_alert(
    request: Request,
    alert_id: int,
    payload: AlertAcknowledge = AlertAcknowledge(),
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return alert_service.acknowledge_alert(db, alert_id, current_user.id, payload.note)

@router.patch("/{alert_id}/assignment", response_model=AlertResponse)
@limiter.limit(WRITE_LIMIT)
def assign_alert(
    request: Request,
    alert_id: int,
    payload: AlertAssignment,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return alert_service.assign_alert(db, alert_id, payload.assigned_to_id, current_user.id)

@router.get("/{alert_id}/history", response_model=list[AlertIncidentEventResponse])
def get_alert_history(
    alert_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return alert_service.list_incident_events(db, alert_id)

@router.get("/{alert_id}/notifications", response_model=list[NotificationLogResponse])
def get_alert_notifications(
    alert_id: int,
    limit: int = alert_service.DEFAULT_ALERT_NOTIFICATIONS_LIMIT,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return alert_service.list_alert_notifications(db, alert_id, limit, offset)
