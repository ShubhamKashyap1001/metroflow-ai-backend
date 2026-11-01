from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.rate_limit import WRITE_LIMIT, limiter
from app.core.security import require_roles
from app.database.session import get_db
from app.enums.crowd_level import CrowdLevel
from app.enums.user_role import UserRole
from app.models.user_profile import UserProfile
from app.schemas.crowd_response import CrowdResponseActionResponse, CrowdResponseCreate, CrowdResponseUpdate
from app.schemas.crowd_log import CrowdLogCreate, CrowdLogResponse
from app.services import crowd_service
from app.services import crowd_response_service
from app.services import peak_hour_service

router = APIRouter(
    prefix="/crowd",
    tags=["Crowd Management"]
)

@router.post("/", response_model=CrowdLogResponse, status_code=201)
@limiter.limit(WRITE_LIMIT)
def log_crowd(request: Request, payload: CrowdLogCreate, db: Session = Depends(get_db)):
    return crowd_service.log_crowd_count(db, payload)

@router.get("/dashboard")
def crowd_dashboard(state: str | None = None, db: Session = Depends(get_db)):
    return crowd_service.get_station_wise_snapshot(db, state)

@router.get("/heatmap")
def crowd_heatmap(
    state: str | None = None,
    limit: int | None = None,
    db: Session = Depends(get_db),
):
    return crowd_service.get_heatmap(db, state, limit)

@router.get("/congestion")
def congestion(
    min_level: CrowdLevel = CrowdLevel.HIGH,
    state: str | None = None,
    db: Session = Depends(get_db),
):
    return crowd_service.get_congested_stations(db, min_level, state)

@router.get("/station-monitor")
def station_monitor(
    state: str | None = None,
    hours: int = 1,
    db: Session = Depends(get_db),
):
    return crowd_service.get_station_monitor(db, state, hours)

@router.get("/peak-hours")
def peak_hours():
    return peak_hour_service.get_cached_peak_hours()

@router.get("/responses", response_model=list[CrowdResponseActionResponse])
def crowd_response_actions(
    state: str | None = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return crowd_response_service.list_actions(db, state)

@router.post("/responses", response_model=CrowdResponseActionResponse, status_code=201)
@limiter.limit(WRITE_LIMIT)
def create_crowd_response(
    request: Request,
    payload: CrowdResponseCreate,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return crowd_response_service.create_action(db, payload, current_user)

@router.patch("/responses/{action_id}", response_model=CrowdResponseActionResponse)
@limiter.limit(WRITE_LIMIT)
def update_crowd_response(
    request: Request,
    action_id: int,
    payload: CrowdResponseUpdate,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return crowd_response_service.update_action(db, action_id, payload, current_user)

@router.delete("/responses/{action_id}", status_code=204)
@limiter.limit(WRITE_LIMIT)
def delete_crowd_response(
    request: Request,
    action_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    crowd_response_service.delete_action(db, action_id, current_user)

@router.get("/{station_id}")
def get_station_crowd(station_id: int, db: Session = Depends(get_db)):
    latest = crowd_service.get_latest_crowd(db, station_id)
    if not latest:
        return {"message": "No crowd data recorded for this station yet"}
    return CrowdLogResponse.model_validate(latest)

@router.get("/{station_id}/inflow-outflow")
def inflow_outflow(station_id: int, hours: int = 24, db: Session = Depends(get_db)):
    return crowd_service.get_inflow_outflow(db, station_id, hours)

@router.get("/{station_id}/analytics")
def station_analytics(station_id: int, hours: int = 24, db: Session = Depends(get_db)):
    return crowd_service.get_station_analytics(db, station_id, hours)
