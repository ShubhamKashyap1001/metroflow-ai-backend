from datetime import date

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.core.rate_limit import limiter
from app.core.security import get_current_user, require_roles
from app.database.session import get_db
from app.models.user_profile import UserProfile
from app.enums.user_role import UserRole
from app.schemas.analytics import PassengerFlowOverview, PassengerTravelUpdates
from app.schemas.prediction import PredictionResponse
from app.schemas.reporting import (
    DataFreshnessReport,
    ReportScheduleCreate,
    ReportScheduleResponse,
    ReportScheduleUpdate,
    TrafficComparisonReport,
)
from app.services import analytics_service
from app.services import report_schedule_service

router = APIRouter(
    prefix="/analytics",
    tags=["Analytics"]
)

@router.get("/traffic-report")
@limiter.limit("20/minute")
def traffic_analysis_report(
    request: Request,
    hours: int = 24,
    state: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    station_id: int | None = None,
    line_name: str | None = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return analytics_service.traffic_analysis_report(
        db, hours, state, start_date, end_date, station_id, line_name
    )

@router.get("/operational-summary")
@limiter.limit("20/minute")
def operational_summary(
    request: Request,
    state: str | None = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return analytics_service.operational_monitoring_summary(db, state)

@router.get("/passenger-flow-overview", response_model=PassengerFlowOverview)
@limiter.limit("20/minute")
def passenger_flow_overview(
    request: Request,
    hours: float = 24,
    top_n: int = 8,
    state: str | None = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return analytics_service.passenger_flow_overview(db, hours, state, top_n)

@router.get("/prediction-insights", response_model=list[PredictionResponse])
@limiter.limit("20/minute")
def prediction_insights(
    request: Request,
    limit: int = 20,
    state: str | None = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return analytics_service.prediction_insights(db, limit, state)

@router.get("/passenger-travel-updates", response_model=PassengerTravelUpdates)
@limiter.limit("20/minute")
def passenger_travel_updates(
    request: Request,
    state: str | None = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return analytics_service.passenger_travel_updates(db, state)

@router.get("/data-freshness", response_model=DataFreshnessReport)
@limiter.limit("20/minute")
def data_freshness(
    request: Request,
    state: str | None = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return analytics_service.data_freshness_report(db, state)

@router.get("/traffic-comparison", response_model=TrafficComparisonReport)
@limiter.limit("20/minute")
def traffic_comparison(
    request: Request,
    start_date: date,
    end_date: date,
    previous_start_date: date,
    previous_end_date: date,
    state: str | None = None,
    station_id: int | None = None,
    line_name: str | None = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return analytics_service.traffic_comparison_report(
        db, start_date, end_date, previous_start_date, previous_end_date, state, station_id, line_name
    )

def _schedule_response(schedule, email: str | None = None) -> dict:
    return {
        "id": schedule.id,
        "frequency": schedule.frequency,
        "delivery_hour_utc": schedule.delivery_hour_utc,
        "day_of_week": schedule.day_of_week,
        "period_days": schedule.period_days,
        "station_id": schedule.station_id,
        "station_name": schedule.station.station_name if schedule.station else None,
        "line_name": schedule.line_name,
        "state": schedule.state,
        "is_active": schedule.is_active,
        "next_run_at": schedule.next_run_at,
        "last_run_at": schedule.last_run_at,
        "last_run_status": schedule.last_run_status,
        "run_attempts": schedule.run_attempts,
        "last_run_error": schedule.last_run_error,
        "delivery_email": email or (schedule.owner.email if schedule.owner else None),
    }

@router.get("/report-schedules", response_model=list[ReportScheduleResponse])
def list_report_schedules(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return [
        _schedule_response(item, current_user.email)
        for item in report_schedule_service.list_user_schedules(db, str(current_user.id))
    ]

@router.post("/report-schedules", response_model=ReportScheduleResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
def create_report_schedule(
    request: Request,
    payload: ReportScheduleCreate,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    item = report_schedule_service.create_schedule(db, str(current_user.id), current_user.email, payload)
    return _schedule_response(item, current_user.email)

@router.patch("/report-schedules/{schedule_id}", response_model=ReportScheduleResponse)
@limiter.limit("10/minute")
def update_report_schedule(
    request: Request,
    schedule_id: int,
    payload: ReportScheduleUpdate,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    item = report_schedule_service.update_schedule(
        db, str(current_user.id), schedule_id, payload.model_dump(exclude_unset=True)
    )
    return _schedule_response(item, current_user.email)

@router.delete("/report-schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
def delete_report_schedule(
    request: Request,
    schedule_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    report_schedule_service.delete_schedule(db, str(current_user.id), schedule_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
