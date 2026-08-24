from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from app.core.security import get_current_user, require_roles
from app.database.session import get_db
from app.enums.user_role import UserRole
from app.models.user_profile import UserProfile
from app.schemas.alert import AlertCreate, AlertResolve, AlertResponse
from app.schemas.notification_log import NotificationLogResponse
from app.services import alert_service

router = APIRouter(
    prefix="/alerts",
    tags=["Alerts"]
)

@router.get("/", response_model=list[AlertResponse])
def get_alerts(
    station_id: int | None = None,
    active_only: bool = False,
    state: str | None = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return alert_service.list_alerts(db, station_id, active_only, state)

@router.post("/", response_model=AlertResponse, status_code=201)
def create_alert(
    payload: AlertCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    alert = alert_service.create_alert(db, payload, created_by=current_user.id)

    if payload.notify_email or payload.notify_sms:
                                                                     
        background_tasks.add_task(
            alert_service.dispatch_alert_notifications,
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
def resolve_alert(
    alert_id: int,
    background_tasks: BackgroundTasks,
    payload: AlertResolve = AlertResolve(),
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    alert = alert_service.resolve_alert(db, alert_id)

    if payload.notify_on_resolve:
                                                                  
        background_tasks.add_task(
            alert_service.dispatch_alert_resolution_notifications,
            alert.id,
            current_user.id,
        )

    return alert

@router.get("/{alert_id}/notifications", response_model=list[NotificationLogResponse])
def get_alert_notifications(
    alert_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    """Per-recipient delivery status for this alert's email + SMS
    dispatch - who was notified on which channel, who failed, and
    why."""
    return alert_service.list_alert_notifications(db, alert_id)
