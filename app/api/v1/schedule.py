from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.enums.day_type import DayType
from app.enums.schedule_status import ScheduleStatus
from app.enums.user_role import UserRole

from app.core.rate_limit import WRITE_LIMIT, limiter
from app.core.security import require_roles
from app.database.session import get_db
from app.models.user_profile import UserProfile
from app.schemas.train_schedule import (
    DelayUpdate,
    FrequencyAdjustment,
    TrainScheduleCreate,
    TrainScheduleResponse,
    TrainScheduleUpdate,
)
from app.services import schedule_service

router = APIRouter(
    prefix="/schedules",
    tags=["Scheduling"]
)

@router.get("/", response_model=list[TrainScheduleResponse])
def list_schedules(
    station_id: int | None = None,
    train_id: int | None = None,
    day_type: DayType | None = None,
    state: str | None = None,
    limit: int = schedule_service.DEFAULT_SCHEDULE_LIST_LIMIT,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    return schedule_service.list_schedules(
        db, station_id, train_id, day_type, state, limit, offset
    )

@router.get("/peak-hours", response_model=list[TrainScheduleResponse])
def peak_hour_schedules(
    station_id: int | None = None,
    state: str | None = None,
    limit: int = schedule_service.DEFAULT_SCHEDULE_LIST_LIMIT,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Peak-hour optimization: currently flagged peak-hour slots."""
    return schedule_service.peak_hour_schedules(db, station_id, state, limit, offset)

@router.get("/delayed", response_model=list[TrainScheduleResponse])
def delayed_schedules(
    station_id: int | None = None,
    state: str | None = None,
    limit: int = schedule_service.DEFAULT_SCHEDULE_LIST_LIMIT,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Delay handling: currently delayed schedule entries."""
    return schedule_service.delayed_schedules(db, station_id, state, limit, offset)

@router.get("/delayed/count")
def delayed_schedules_count(
    station_id: int | None = None,
    state: str | None = None,
    db: Session = Depends(get_db),
):
    """Lightweight counterpart to /delayed - total count of currently
    delayed schedules (same filters), for the dashboard's Recent Alerts
    widget, which only needs the number rather than the capped list."""
    return {"count": schedule_service.delayed_schedules_count(db, station_id, state)}

@router.get("/upcoming")
def upcoming_schedules(
    state: str | None = None,
    status: ScheduleStatus | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
):
    """Feed for the dashboard's "Upcoming Train Schedule" widget - next
    departures with train, line, from/to stations and live status.
    `state` scopes to one city, `status` filters to on_time/delayed."""
    return schedule_service.get_upcoming_schedules(db, state, status, limit)

@router.get("/{schedule_id}", response_model=TrainScheduleResponse)
def get_schedule(schedule_id: int, db: Session = Depends(get_db)):
    return schedule_service.get_schedule(db, schedule_id)

@router.post("/", response_model=TrainScheduleResponse, status_code=201)
@limiter.limit(WRITE_LIMIT)
def create_schedule(
    request: Request,
    payload: TrainScheduleCreate,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return schedule_service.create_schedule(db, payload)

@router.put("/{schedule_id}", response_model=TrainScheduleResponse)
@limiter.limit(WRITE_LIMIT)
def update_schedule(
    request: Request,
    schedule_id: int,
    payload: TrainScheduleUpdate,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return schedule_service.update_schedule(db, schedule_id, payload)

@router.patch("/{schedule_id}/delay", response_model=TrainScheduleResponse)
@limiter.limit(WRITE_LIMIT)
def report_delay(
    request: Request,
    schedule_id: int,
    payload: DelayUpdate,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    """Delay handling workflow."""
    return schedule_service.handle_delay(db, schedule_id, payload)

@router.patch("/{schedule_id}/frequency", response_model=TrainScheduleResponse)
@limiter.limit(WRITE_LIMIT)
def adjust_frequency(
    request: Request,
    schedule_id: int,
    payload: FrequencyAdjustment,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    """Frequency adjustment workflow."""
    return schedule_service.adjust_frequency(db, schedule_id, payload)