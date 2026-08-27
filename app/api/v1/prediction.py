"""Milestone 2 - AI Prediction Module API.

Crowd prediction models, passenger demand forecasting, traffic
pattern analysis, smart recommendations. Every route requires a
logged-in user and is rate-limited (20/minute per IP) since these all
run an ML model - unauthenticated/unlimited access to compute-heavy
endpoints is an easy way to overload the server.
"""
from fastapi import APIRouter, Depends, Request
from slowapi import Limiter
from slowapi.util import get_ipaddr
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.database.session import get_db
from app.models.user_profile import UserProfile
from app.schemas.prediction import (
    AggregateTrafficPattern,
    CrowdModelMetrics,
    CrowdPredictionRequest,
    DelayPredictionRequest,
    DemandForecastRequest,
    FrequencyRecommendationRequest,
    PredictionResponse,
    RegressionModelMetrics,
    SmartRecommendation,
)
from app.services import prediction_service

router = APIRouter(
    prefix="/predictions",
    tags=["AI Prediction"]
)

limiter = Limiter(key_func=get_ipaddr)

@router.post("/crowd", response_model=PredictionResponse)
@limiter.limit("20/minute")
def predict_crowd(
    request: Request,
    payload: CrowdPredictionRequest,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    """Crowd prediction models: passenger density estimation."""
    return prediction_service.forecast_crowd(db, payload.station_id, payload.target_datetime)

@router.get("/crowd/metrics", response_model=CrowdModelMetrics)
@limiter.limit("20/minute")
def crowd_model_metrics(
    request: Request,
    current_user: UserProfile = Depends(get_current_user),
):
    """New: live evaluation metrics for the production crowd/demand model -
    accuracy, MAE/MAPE/R2, confusion matrix and feature importance,
    computed from the real passenger_flow.csv held-out test set. Powers
    the AI Prediction dashboard page."""
    return prediction_service.get_crowd_model_metrics()

@router.post("/demand", response_model=list[PredictionResponse])
@limiter.limit("20/minute")
def forecast_demand(
    request: Request,
    payload: DemandForecastRequest,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    """Passenger demand forecasting, hour-by-hour."""
    return prediction_service.forecast_demand(db, payload.station_id, payload.hours_ahead)

@router.post("/delay", response_model=PredictionResponse)
@limiter.limit("20/minute")
def predict_delay(
    request: Request,
    payload: DelayPredictionRequest,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    """Delay impact prediction."""
    return prediction_service.forecast_delay(db, payload.train_id, payload.station_id)

@router.get("/delay/metrics", response_model=RegressionModelMetrics)
@limiter.limit("20/minute")
def delay_model_metrics(
    request: Request,
    current_user: UserProfile = Depends(get_current_user),
):
    """New: live evaluation metrics for the production delay model -
    MAE/MAPE/R2 and feature importance, computed from the real
    train_operations.csv held-out test set. Powers the AI Prediction
    dashboard page."""
    return prediction_service.get_delay_model_metrics()

@router.post("/frequency", response_model=PredictionResponse)
@limiter.limit("20/minute")
def recommend_frequency(
    request: Request,
    payload: FrequencyRecommendationRequest,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    """Train frequency recommendations / resource utilization optimization."""
    return prediction_service.recommend_train_frequency(db, payload.station_id, payload.is_peak_hour)

@router.get("/frequency/metrics", response_model=RegressionModelMetrics)
@limiter.limit("20/minute")
def frequency_model_metrics(
    request: Request,
    current_user: UserProfile = Depends(get_current_user),
):
    """New: live evaluation metrics for the production train-frequency
    recommendation model - MAE/MAPE/R2 and feature importance, computed
    from the real passenger_flow.csv held-out test set. Powers the AI
    Prediction dashboard page."""
    return prediction_service.get_frequency_model_metrics()

@router.get("/traffic-pattern/{station_id}")
@limiter.limit("20/minute")
def traffic_pattern(
    request: Request,
    station_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    """Traffic pattern analysis: 24h predicted demand curve."""
    return prediction_service.traffic_pattern_analysis(db, station_id)

@router.get("/traffic-pattern-aggregate/all", response_model=AggregateTrafficPattern)
@limiter.limit("20/minute")
def traffic_pattern_aggregate(
    request: Request,
    state: str | None = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    """24h predicted demand curve summed across every station (optionally
    scoped to `state`), in a single rate-limited call - what the
    "Passenger Analytics" dashboard widget needs, without it having to
    fan out one request per station (see prediction_service.
    all_stations_traffic_pattern for why that used to fail)."""
    return prediction_service.all_stations_traffic_pattern(db, state)

@router.get("/recommendations/{station_id}", response_model=list[SmartRecommendation])
@limiter.limit("20/minute")
def recommendations(
    request: Request,
    station_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    """Smart recommendations combining crowd, delay and frequency predictions."""
    return prediction_service.smart_recommendations(db, station_id)