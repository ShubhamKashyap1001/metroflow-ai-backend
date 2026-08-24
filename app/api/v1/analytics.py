"""Analytics endpoints. Traffic analysis reports (Milestone 2) plus
station performance / operational monitoring / AI insight groundwork
for the full Milestone 3 Analytics Dashboard Module. Every route now
requires a logged-in user and is rate-limited (20/minute per IP),
same treatment as app/api/v1/prediction.py - these aggregate over the
whole dataset and shouldn't be open to anonymous/unlimited traffic.
"""
from fastapi import APIRouter, Depends, Request
from slowapi import Limiter
from slowapi.util import get_ipaddr
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.database.session import get_db
from app.models.user_profile import UserProfile
from app.schemas.analytics import PassengerFlowOverview
from app.schemas.prediction import PredictionResponse
from app.services import analytics_service

router = APIRouter(
    prefix="/analytics",
    tags=["Analytics"]
)

limiter = Limiter(key_func=get_ipaddr)

@router.get("/traffic-report")
@limiter.limit("20/minute")
def traffic_analysis_report(
    request: Request,
    hours: int = 24,
    state: str | None = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    """Traffic analysis reports (Milestone 2 outcome)."""
    return analytics_service.traffic_analysis_report(db, hours, state)

@router.get("/operational-summary")
@limiter.limit("20/minute")
def operational_summary(
    request: Request,
    state: str | None = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
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
    """Passenger inflow/outflow KPIs, top stations by inflow, and
    ridership-by-line - powers the Analytics page's KPI cards and the
    "Passenger Flow by Station" / "Ridership by Line" widgets."""
    return analytics_service.passenger_flow_overview(db, hours, state, top_n)

@router.get("/prediction-insights", response_model=list[PredictionResponse])
@limiter.limit("20/minute")
def prediction_insights(
    request: Request,
    limit: int = 20,
    state: str | None = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    """AI prediction insights feed for the analytics dashboard."""
    return analytics_service.prediction_insights(db, limit, state)
