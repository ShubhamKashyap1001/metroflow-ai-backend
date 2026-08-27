from datetime import datetime, timedelta
from typing import Callable

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.ai_engine.prediction.crowd_predictor import predict_crowd
from app.ai_engine.prediction.delay_predictor import predict_delay
from app.ai_engine.prediction.frequency_predictor import recommend_frequency
from app.core import cache
from app.enums.crowd_level import CrowdLevel
from app.enums.prediction_type import PredictionType
from app.models.prediction import Prediction
from app.models.station import Station

PREDICTION_CACHE_TTL_SECONDS = 300

PREDICTION_TTL = PREDICTION_CACHE_TTL_SECONDS

def _cached_prediction(cache_key: str, compute: Callable[[], dict]) -> dict:
    """Redis-first wrapper around a `predict_*`/`recommend_*` call.
    Fail-open like the rest of the app's caching (app/core/cache.py):
    a cache miss or a down Redis just falls through to actually
    running the model, same result either way, just slower."""
    cached = cache.get_json(cache_key)
    if cached is not None:
        if cached.get("target_datetime"):
            cached["target_datetime"] = datetime.fromisoformat(cached["target_datetime"])
        return cached

    result = compute()
    cache.set_json(cache_key, result, ttl_seconds=PREDICTION_CACHE_TTL_SECONDS)
    return result

def _cached_predict_crowd(station_id: int, target_datetime: datetime, light: bool = False) -> dict:
    key = f"predict:crowd:{station_id}:{target_datetime.isoformat()}:{light}"
    return _cached_prediction(key, lambda: predict_crowd(station_id, target_datetime, light=light))

def _cached_predict_delay(db: Session, station_id: int, target_datetime: datetime, train_id: int | None = None) -> dict:
                                                                     
    key = f"predict:delay:{station_id}:{target_datetime.isoformat()}:{train_id}"
    return _cached_prediction(key, lambda: predict_delay(station_id, target_datetime, train_id=train_id, db=db))

def _cached_recommend_frequency(station_id: int, target_datetime: datetime) -> dict:
    key = f"predict:frequency:{station_id}:{target_datetime.isoformat()}"
    return _cached_prediction(key, lambda: recommend_frequency(station_id, target_datetime))

def _save_prediction(
    db: Session,
    station_id: int,
    prediction_type: PredictionType,
    predicted_value: float,
    confidence: float,
    target_datetime: datetime,
    model_version: str,
    commit: bool = True,
) -> Prediction:
    record = Prediction(
        station_id=station_id,
        prediction_type=prediction_type,
        predicted_value=predicted_value,
        predicted_count=round(predicted_value) if prediction_type in (
            PredictionType.CROWD, PredictionType.DEMAND
        ) else None,
        confidence=confidence,
        target_datetime=target_datetime,
        model_version=model_version,
    )
    db.add(record)
                                                                   
    if commit:
        db.commit()
        db.refresh(record)
    return record

def _require_station(db: Session, station_id: int) -> Station:
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    return station

def forecast_crowd(db: Session, station_id: int, target_datetime: datetime | None = None) -> Prediction:
    """Crowd prediction models."""
    _require_station(db, station_id)
    dt = target_datetime or datetime.utcnow()
    result = _cached_predict_crowd(station_id, dt)
    record = _save_prediction(
        db,
        station_id=station_id,
        prediction_type=PredictionType.CROWD,
        predicted_value=result["predicted_count"],
        confidence=result["confidence"],
        target_datetime=result["target_datetime"],
        model_version=result["model_version"],
    )
    # Per-candidate breakdown (random_forest/xgboost) isn't a DB
    # column - Prediction only ever stores the winning model's value -
    # so it's attached to the already-saved/refreshed record here,
    # purely for this response, instead of being silently dropped.
    record.models = result.get("models", {})
    return record

def forecast_demand(db: Session, station_id: int, hours_ahead: int = 6) -> list[Prediction]:
    """Passenger demand forecasting: hour-by-hour for the next N hours."""
    _require_station(db, station_id)
    now = datetime.utcnow()
    records = []
    for i in range(1, hours_ahead + 1):
        target = now + timedelta(hours=i)
        result = _cached_predict_crowd(station_id, target)
        record = _save_prediction(
            db,
            station_id=station_id,
            prediction_type=PredictionType.DEMAND,
            predicted_value=result["predicted_count"],
            confidence=result["confidence"],
            target_datetime=result["target_datetime"],
            model_version=result["model_version"],
            commit=False,
        )
        records.append(record)
                                                                
    db.commit()
    for record in records:
        db.refresh(record)
    return records

def forecast_delay(db: Session, train_id: int, station_id: int) -> Prediction:
    """Delay impact prediction, feeding the Scheduling Management Module."""
    _require_station(db, station_id)
    dt = datetime.utcnow()
    result = _cached_predict_delay(db, station_id, dt, train_id=train_id)
    record = _save_prediction(
        db,
        station_id=station_id,
        prediction_type=PredictionType.DELAY,
        predicted_value=result["predicted_delay_minutes"],
        confidence=0.7,
        target_datetime=result["target_datetime"],
        model_version=result["model_version"],
    )
    # Same as forecast_crowd above: attach the random_forest/xgboost
    # breakdown that predict_delay() already computes but that has no
    # DB column to live in, so the API response can carry it too.
    record.models = result.get("models", {})
    return record

def recommend_train_frequency(db: Session, station_id: int, is_peak_hour: bool = False) -> Prediction:
    """Train frequency recommendations / resource utilization optimization."""
    _require_station(db, station_id)
    target = datetime.utcnow()
    if is_peak_hour:
        target = target.replace(hour=9)                                  
    result = _cached_recommend_frequency(station_id, target)
    record = _save_prediction(
        db,
        station_id=station_id,
        prediction_type=PredictionType.FREQUENCY,
        predicted_value=result["recommended_frequency_minutes"],
        confidence=0.75,
        target_datetime=result["target_datetime"],
        model_version=result["model_version"],
    )
    # Same as forecast_crowd above: attach the random_forest/xgboost
    # breakdown that recommend_frequency() already computes but that
    # has no DB column to live in, so the API response can carry it too.
    record.models = result.get("models", {})
    return record

def traffic_pattern_analysis(db: Session, station_id: int) -> dict:
    """Traffic pattern analysis: 24h predicted demand curve for a station."""
    _require_station(db, station_id)
    now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    curve = []
    for hour in range(24):
        target = now.replace(hour=hour)
                                                                       
        result = _cached_predict_crowd(station_id, target, light=True)
        curve.append({
            "hour": hour,
            "predicted_count": result["predicted_count"],
            "is_peak_hour": 8 <= hour <= 11 or 17 <= hour <= 20,
        })

    peak = max(curve, key=lambda c: c["predicted_count"])
    trough = min(curve, key=lambda c: c["predicted_count"])

    return {
        "station_id": station_id,
        "hourly_forecast": curve,
        "peak_hour": peak["hour"],
        "peak_predicted_count": peak["predicted_count"],
        "quietest_hour": trough["hour"],
    }

def all_stations_traffic_pattern(db: Session, state: str | None = None) -> dict:
    """Same 24h predicted-demand curve as traffic_pattern_analysis, but
    summed across every station (optionally scoped to `state`) in ONE
    call, instead of the caller having to fan out a separate request
    per station.

    This exists because the "Passenger Analytics" dashboard widget
    needs an all-stations total, and was previously built by having
    the FRONTEND call GET /traffic-pattern/{station_id} once per
    station via Promise.all() - one HTTP request, one DB round trip,
    and 24 model lookups PER STATION, all fired in parallel on every
    page load. Two compounding problems with that:

    1. This endpoint is rate-limited at 20/minute per IP (see the
       module docstring in api/v1/prediction.py - deliberate, since
       every call here can run an ML model). A city with more than
       ~20 stations blew straight through that limit on a single page
       load, so most of those per-station requests came back 429 and
       the widget surfaced "Couldn't load the forecast right now" -
       not because the prediction service was actually slow or down,
       but because the page itself DDoS'd its own rate limiter.
    2. Even the requests that didn't get rate-limited meant dozens of
       concurrent DB session / ML calls competing for the same
       process, which is what made the ones that DID succeed take
       far longer than a single request should.

    Computing the sum server-side keeps the exact same per-station,
    per-hour Redis-cached prediction calls (so the actual compute cost
    doesn't change and every value stays independently verifiable),
    but collapses N HTTP requests and N rate-limit hits into 1.
    """
                                                                   
    cache_key = f"predict:traffic-pattern-aggregate:{state or 'all'}"
    cached = cache.get_json(cache_key)
    if cached is not None:
        return cached

    from app.ai_engine.prediction.crowd_predictor import predict_crowd_bulk
    from app.utils.geo import cities_for_state

    cities = cities_for_state(state)
    query = db.query(Station)
    if cities:
        query = query.filter(Station.city.in_(cities))
    stations = query.all()

    now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    station_ids = [s.id for s in stations]
    hours = list(range(24))

    # One vectorized model call for every (station, hour) pair instead
    # of 24 x len(stations) individual predict_crowd() calls in a loop
    # (see predict_crowd_bulk's docstring) - this is what was making
    # this endpoint slow enough to time out on a cache-cold request.
    bulk_results = predict_crowd_bulk(station_ids, hours, now)

    totals = [0] * 24
    for station_id in station_ids:
        for hour in hours:
            totals[hour] += bulk_results[(station_id, hour)]["predicted_count"]

    curve = [
        {
            "hour": hour,
            "predicted_count": totals[hour],
            "is_peak_hour": 8 <= hour <= 11 or 17 <= hour <= 20,
        }
        for hour in range(24)
    ]
    peak = max(curve, key=lambda c: c["predicted_count"])
    trough = min(curve, key=lambda c: c["predicted_count"])

    response = {
        "station_count": len(stations),
        "hourly_forecast": curve,
        "peak_hour": peak["hour"],
        "peak_predicted_count": peak["predicted_count"],
        "quietest_hour": trough["hour"],
    }
    cache.set_json(cache_key, response, ttl_seconds=PREDICTION_CACHE_TTL_SECONDS)
    return response

def smart_recommendations(db: Session, station_id: int) -> list[dict]:
    """Smart recommendations combining crowd + delay + frequency predictions."""
    dt = datetime.utcnow()
    crowd = _cached_predict_crowd(station_id, dt)
    delay = _cached_predict_delay(db, station_id, dt)
    frequency = _cached_recommend_frequency(station_id, dt)

    # This is the one prediction path the frontend calls automatically
    # (AIInsights.tsx -> getRecommendations, on every dashboard load and
    # on every live crowd/delay/station event) rather than only on an
    # explicit user action like the crowd/delay/frequency POST endpoints
    # above. Persist the same three predictions those endpoints already
    # save via _save_prediction, so the Activity Timeline widget (which
    # just reads the latest rows from this table) actually has real
    # activity to show instead of always being empty.
    _save_prediction(
        db,
        station_id=station_id,
        prediction_type=PredictionType.CROWD,
        predicted_value=crowd["predicted_count"],
        confidence=crowd["confidence"],
        target_datetime=crowd["target_datetime"],
        model_version=crowd["model_version"],
        commit=False,
    )
    _save_prediction(
        db,
        station_id=station_id,
        prediction_type=PredictionType.DELAY,
        predicted_value=delay["predicted_delay_minutes"],
        confidence=0.7,
        target_datetime=delay["target_datetime"],
        model_version=delay["model_version"],
        commit=False,
    )
    _save_prediction(
        db,
        station_id=station_id,
        prediction_type=PredictionType.FREQUENCY,
        predicted_value=frequency["recommended_frequency_minutes"],
        confidence=0.75,
        target_datetime=frequency["target_datetime"],
        model_version=frequency["model_version"],
        commit=False,
    )
    db.commit()

    recommendations = []

    if crowd["predicted_count"] > 800:
        recommendations.append({
            "station_id": station_id,
            "title": "High crowd expected",
            "detail": f"Predicted ~{crowd['predicted_count']} passengers. "
                      f"Consider deploying additional staff and opening extra gates.",
            "severity": "warning",
        })

    if delay["predicted_delay_minutes"] > 3:
        recommendations.append({
            "station_id": station_id,
            "title": "Delay risk",
            "detail": f"Model predicts ~{delay['predicted_delay_minutes']} min of delay "
                      f"driven by current congestion levels.",
            "severity": "warning",
        })

    recommendations.append({
        "station_id": station_id,
        "title": "Frequency suggestion",
        "detail": f"Recommended train interval: {frequency['recommended_frequency_minutes']} min "
                  f"({'peak' if frequency['is_peak_hour'] else 'off-peak'} slot).",
        "severity": "info",
    })

    if not recommendations:
        recommendations.append({
            "station_id": station_id,
            "title": "Normal operations",
            "detail": "No anomalies detected; current schedule and staffing look adequate.",
            "severity": "info",
        })

    return recommendations

def get_crowd_model_metrics() -> dict:
    """New: live evaluation metrics for the production crowd/demand model
    (same trained model backs both - see forecast_demand above),
    computed from the real passenger_flow.csv held-out test split. Powers
    the AI Prediction dashboard page."""
    from app.ai_engine.prediction.crowd_metrics import compute_crowd_metrics

    return compute_crowd_metrics()

def get_delay_model_metrics() -> dict:
    """New: live evaluation metrics for the production delay model,
    computed from the real train_operations.csv held-out test split.
    Same role as get_crowd_model_metrics above; powers the delay
    section of the AI Prediction dashboard page."""
    from app.ai_engine.prediction.delay_metrics import compute_delay_metrics

    return compute_delay_metrics()

def get_frequency_model_metrics() -> dict:
    """New: live evaluation metrics for the production train-frequency
    recommendation model, computed from the real passenger_flow.csv
    held-out test split. Same role as get_crowd_model_metrics above;
    powers the frequency section of the AI Prediction dashboard page."""
    from app.ai_engine.prediction.frequency_metrics import compute_frequency_metrics

    return compute_frequency_metrics()