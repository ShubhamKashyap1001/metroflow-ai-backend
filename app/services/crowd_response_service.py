import logging

from fastapi import HTTPException
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import Session

from app.enums.alert_type import AlertType
from app.enums.crowd_level import CrowdLevel
from app.enums.notification_source import NotificationSource
from app.enums.user_role import UserRole
from app.models.alert import Alert
from app.models.crowd_response_action import CrowdResponseAction
from app.models.station import Station
from app.models.user_profile import UserProfile
from app.schemas.alert import AlertCreate
from app.schemas.crowd_response import (
    CrowdResponseActionResponse,
    CrowdResponseCreate,
    CrowdResponseUpdate,
)
from app.services import alert_service, crowd_service, notification_service
from app.utils.geo import cities_for_state

logger = logging.getLogger(__name__)

def list_actions(db: Session, state: str | None = None) -> list[CrowdResponseAction]:
    query = db.query(CrowdResponseAction).options(
        joinedload(CrowdResponseAction.station),
        joinedload(CrowdResponseAction.assignee),
    )
    cities = cities_for_state(state)
    if cities:
        query = query.join(Station, Station.id == CrowdResponseAction.station_id).filter(Station.city.in_(cities))
    return query.order_by(CrowdResponseAction.created_at.desc()).limit(100).all()

def create_action(
    db: Session,
    payload: CrowdResponseCreate,
    actor: UserProfile,
) -> CrowdResponseActionResponse:
    station = db.get(Station, payload.station_id)
    if not station or not station.is_active:
        raise HTTPException(status_code=404, detail="Active station not found")
    crowd = crowd_service.get_latest_crowd(db, payload.station_id)
    if not crowd:
        raise HTTPException(status_code=409, detail="No current crowd reading is available for this station")
    level = crowd.get("crowd_level")
    if level not in (CrowdLevel.HIGH, CrowdLevel.CRITICAL, "high", "critical"):
        raise HTTPException(status_code=409, detail="A crowd response can only be opened for a high or critical station")

    if payload.action_type == "staff_dispatch":
        already_open = db.query(CrowdResponseAction.id).filter(
            CrowdResponseAction.station_id == station.id,
            CrowdResponseAction.action_type == "staff_dispatch",
            CrowdResponseAction.status.in_(["open", "in_progress"]),
        ).first()
        if already_open:
            raise HTTPException(status_code=409, detail="An active staff response already exists for this station")
    else:
        already_published = (
            db.query(CrowdResponseAction.id)
            .join(Alert, Alert.id == CrowdResponseAction.linked_alert_id)
            .filter(
                CrowdResponseAction.station_id == station.id,
                CrowdResponseAction.action_type == "passenger_advisory",
                Alert.is_resolved.is_(False),
            )
            .first()
        )
        if already_published:
            raise HTTPException(status_code=409, detail="An active passenger advisory already exists for this station")

    count = int(crowd.get("current_count") or 0)
    occupancy = round(count / max(station.capacity or 1, 1) * 100)
    linked_alert_id = None
    if payload.action_type == "passenger_advisory":
        message = (
            f"Passenger advisory: {station.station_name} is currently {level.value if hasattr(level, 'value') else level} "
            f"({occupancy}% estimated capacity). Please follow station staff guidance and consider a nearby alternative."
        )
        alert = alert_service.create_alert(
            db,
            AlertCreate(
                station_id=station.id,
                alert_type=AlertType.OVERCROWDING,
                message=message[:500],
                notify_email=False,
                notify_sms=False,
            ),
            created_by=actor.id,
        )
        linked_alert_id = alert.id
        status = "completed"
    else:
        message = f"Staff response requested at {station.station_name}: check platform and entry congestion ({occupancy}% estimated capacity)."
        status = "open"

    action = CrowdResponseAction(
        station_id=station.id,
        action_type=payload.action_type,
        status=status,
        message=message,
        created_by_id=actor.id,
        linked_alert_id=linked_alert_id,
    )
    db.add(action)
    db.commit()
    db.refresh(action)

    response = CrowdResponseActionResponse(
        id=action.id,
        station_id=station.id,
        station_name=station.station_name,
        action_type=action.action_type,
        status=action.status,
        message=action.message,
        created_by_id=str(action.created_by_id),
        assigned_to_id=None,
        assignee_name=None,
        linked_alert_id=action.linked_alert_id,
        notifications_delivered=None,
        created_at=action.created_at,
        updated_at=action.updated_at,
    )

    if payload.action_type == "staff_dispatch":
        notifications_delivered = True
        staff = (
            db.query(UserProfile)
            .filter(UserProfile.is_active.is_(True), UserProfile.role.in_([UserRole.ADMIN, UserRole.OPERATOR]))
            .all()
        )
        for recipient in staff:
            try:
                notification_service.create_notification(
                    db,
                    source=NotificationSource.OPERATOR,
                    title=f"Staff response requested · {station.station_name}",
                    message=message,
                    user_id=recipient.id,
                    state=station.city,
                )
            except Exception:
                notifications_delivered = False
                logger.exception(
                    "Staff response %s was saved, but notification delivery failed for operator %s",
                    action.id,
                    recipient.id,
                )
                try:
                    db.rollback()
                except Exception:
                    logger.exception("Could not reset the session after notification delivery failed")
        response.notifications_delivered = notifications_delivered
    return response

def update_action(db: Session, action_id: int, payload: CrowdResponseUpdate, actor: UserProfile) -> CrowdResponseAction:
    action = db.get(CrowdResponseAction, action_id)
    if not action:
        raise HTTPException(status_code=404, detail="Crowd response action not found")
    if action.status in ("completed", "cancelled"):
        raise HTTPException(status_code=409, detail="This response action is already closed")
    if action.assigned_to_id and str(action.assigned_to_id) != str(actor.id) and actor.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="This response action is assigned to another operator")
    if payload.assign_to_me:
        if action.assigned_to_id and str(action.assigned_to_id) != str(actor.id) and actor.role != UserRole.ADMIN:
            raise HTTPException(status_code=409, detail="This response action is assigned to another operator")
        action.assigned_to_id = actor.id
    if payload.status:
        if payload.status == "in_progress" and action.assigned_to_id and str(action.assigned_to_id) != str(actor.id) and actor.role != UserRole.ADMIN:
            raise HTTPException(status_code=403, detail="Assign this response to yourself before starting it")
        if payload.status == "completed" and action.status == "open":
            raise HTTPException(status_code=409, detail="Start the response before marking it completed")
        action.status = payload.status
    db.commit()
    db.refresh(action)
    return action

def delete_action(db: Session, action_id: int, actor: UserProfile) -> None:
    action = db.get(CrowdResponseAction, action_id)
    if not action:
        raise HTTPException(status_code=404, detail="Crowd response action not found")
    if action.status not in ("completed", "cancelled"):
        raise HTTPException(status_code=409, detail="Only closed response actions can be removed")
    if actor.role != UserRole.ADMIN:
        is_owner = (
            action.action_type == "staff_dispatch"
            and action.assigned_to_id
            and str(action.assigned_to_id) == str(actor.id)
        ) or (
            action.action_type == "passenger_advisory"
            and str(action.created_by_id) == str(actor.id)
        )
        if not is_owner:
            raise HTTPException(status_code=403, detail="Only the assigned operator or administrator can remove this response")
    if action.action_type == "passenger_advisory" and action.linked_alert_id:
        alert_service.resolve_alert(db, action.linked_alert_id, actor_id=actor.id)
    db.delete(action)
    db.commit()
