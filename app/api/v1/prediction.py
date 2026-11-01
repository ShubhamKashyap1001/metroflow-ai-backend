from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.rate_limit import limiter
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

@router.post("/crowd", response_model=PredictionResponse)
@limiter.limit("20/minute")
def predict_crowd(
    request: Request,
    payload: CrowdPredictionRequest,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return prediction_service.forecast_crowd(db, payload.station_id, payload.target_datetime)

@router.get("/crowd/metrics", response_model=CrowdModelMetrics)
@limiter.limit("20/minute")
def crowd_model_metrics(
    request: Request,
    current_user: UserProfile = Depends(get_current_user),
):
    return prediction_service.get_crowd_model_metrics()

@router.get("/crowd/operational-metrics")
@limiter.limit("5/minute")
def crowd_operational_metrics(
    request: Request,
    lookback_days: int = Query(30, ge=1, le=90),
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return prediction_service.get_sensor_validated_crowd_metrics(db, lookback_days)

@router.post("/demand", response_model=list[PredictionResponse])
@limiter.limit("20/minute")
def forecast_demand(
    request: Request,
    payload: DemandForecastRequest,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return prediction_service.forecast_demand(db, payload.station_id, payload.hours_ahead)

@router.post("/delay", response_model=PredictionResponse)
@limiter.limit("20/minute")
def predict_delay(
    request: Request,
    payload: DelayPredictionRequest,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return prediction_service.forecast_delay(db, payload.train_id, payload.station_id)

@router.get("/delay/metrics", response_model=RegressionModelMetrics)
@limiter.limit("20/minute")
def delay_model_metrics(
    request: Request,
    current_user: UserProfile = Depends(get_current_user),
):
    return prediction_service.get_delay_model_metrics()

@router.post("/frequency", response_model=PredictionResponse)
@limiter.limit("20/minute")
def recommend_frequency(
    request: Request,
    payload: FrequencyRecommendationRequest,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return prediction_service.recommend_train_frequency(db, payload.station_id, payload.is_peak_hour)

@router.get("/frequency/metrics", response_model=RegressionModelMetrics)
@limiter.limit("20/minute")
def frequency_model_metrics(
    request: Request,
    current_user: UserProfile = Depends(get_current_user),
):
    return prediction_service.get_frequency_model_metrics()

@router.get("/traffic-pattern/{station_id}")
@limiter.limit("20/minute")
def traffic_pattern(
    request: Request,
    station_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return prediction_service.traffic_pattern_analysis(db, station_id)

@router.get("/traffic-pattern-aggregate/all", response_model=AggregateTrafficPattern)
@limiter.limit("20/minute")
def traffic_pattern_aggregate(
    request: Request,
    state: str | None = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return prediction_service.all_stations_traffic_pattern(db, state)

MAX_BULK_RECOMMENDATION_STATIONS = 30

@router.get("/recommendations/bulk", response_model=dict[int, list[SmartRecommendation]])
@limiter.limit("20/minute")
def recommendations_bulk(
    request: Request,
    station_ids: str,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    try:
        ids = [int(s) for s in station_ids.split(",") if s.strip() != ""]
    except ValueError:
        raise HTTPException(status_code=422, detail="station_ids must be a comma-separated list of integers")

    if not ids:
        return {}

    if len(ids) > MAX_BULK_RECOMMENDATION_STATIONS:
        raise HTTPException(
            status_code=422,
            detail=f"station_ids supports at most {MAX_BULK_RECOMMENDATION_STATIONS} stations per call",
        )

    return prediction_service.smart_recommendations_bulk(db, ids)

@router.get("/recommendations/{station_id}", response_model=list[SmartRecommendation])
@limiter.limit("20/minute")
def recommendations(
    request: Request,
    station_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return prediction_service.smart_recommendations(db, station_id)
