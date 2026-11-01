from datetime import datetime, timedelta, timezone
from typing import Callable
import time

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.ai_engine.prediction.crowd_predictor import (
    dataset_hourly_passenger_baseline,
    dataset_station_id_map,
    predict_crowd,
    predict_crowd_bulk,
)
from app.ai_engine.prediction.delay_predictor import predict_delay
from app.ai_engine.prediction.frequency_predictor import recommend_frequency
from app.core import cache
from app.enums.crowd_level import CrowdLevel
from app.enums.prediction_type import PredictionType
from app.models.prediction import Prediction
from app.models.station import Station
from app.models.sensor_feed_event import SensorFeedEvent
from app.utils.timezone import to_business_time

PREDICTION_CACHE_TTL_SECONDS = 300

PREDICTION_TTL = PREDICTION_CACHE_TTL_SECONDS

DEFAULT_CAPACITY_FALLBACK = 2400

MAX_DEMAND_FORECAST_HOURS_AHEAD = 168

def _cached_prediction(cache_key: str, compute: Callable[[], dict]) -> dict:
    cached = cache.get_json(cache_key)
    if cached is not None:
        if cached.get("target_datetime"):
            cached["target_datetime"] = datetime.fromisoformat(cached["target_datetime"])
        return cached

    result = compute()
    cache.set_json(cache_key, result, ttl_seconds=PREDICTION_CACHE_TTL_SECONDS)
    return result

def _cache_key_bucket(target_datetime: datetime) -> str:
    return target_datetime.replace(minute=0, second=0, microsecond=0).isoformat()

def _crowd_cache_key(station_id: int, target_datetime: datetime, light: bool = False) -> str:
    return f"predict:crowd:{station_id}:{_cache_key_bucket(target_datetime)}:{light}"

def _delay_cache_key(station_id: int, target_datetime: datetime, train_id: int | None = None) -> str:
    return f"predict:delay:{station_id}:{_cache_key_bucket(target_datetime)}:{train_id}"

def _frequency_cache_key(station_id: int, target_datetime: datetime) -> str:
    return f"predict:frequency:{station_id}:{_cache_key_bucket(target_datetime)}"

def _cached_predict_crowd(station_id: int, target_datetime: datetime, light: bool = False) -> dict:
    key = _crowd_cache_key(station_id, target_datetime, light)
    return _cached_prediction(key, lambda: predict_crowd(station_id, target_datetime, light=light))

def _cached_predict_delay(db: Session, station_id: int, target_datetime: datetime, train_id: int | None = None) -> dict:
                                                                     
    key = _delay_cache_key(station_id, target_datetime, train_id)
    return _cached_prediction(key, lambda: predict_delay(station_id, target_datetime, train_id=train_id, db=db))

def _cached_recommend_frequency(station_id: int, target_datetime: datetime) -> dict:
    key = _frequency_cache_key(station_id, target_datetime)
    return _cached_prediction(key, lambda: recommend_frequency(station_id, target_datetime))

def _save_prediction(
    db: Session,
    station_id: int,
    prediction_type: PredictionType,
    predicted_value: float,
    confidence: float | None,
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

PREDICTION_WRITE_DEDUPE_SECONDS = PREDICTION_CACHE_TTL_SECONDS

def _get_or_save_prediction(
    db: Session,
    *,
    station_id: int,
    prediction_type: PredictionType,
    predicted_value: float,
    confidence: float | None,
    target_datetime: datetime,
    model_version: str,
    commit: bool = True,
) -> Prediction:
    dedupe_key = (
        f"predictions:write:{prediction_type.value}:{station_id}:"
        f"{target_datetime.isoformat() if hasattr(target_datetime, 'isoformat') else target_datetime}"
    )

    if cache.set_nx(dedupe_key, PREDICTION_WRITE_DEDUPE_SECONDS):
        return _save_prediction(
            db,
            station_id=station_id,
            prediction_type=prediction_type,
            predicted_value=predicted_value,
            confidence=confidence,
            target_datetime=target_datetime,
            model_version=model_version,
            commit=commit,
        )

    for attempt in range(3):
        existing = (
            db.query(Prediction)
            .filter(
                Prediction.station_id == station_id,
                Prediction.prediction_type == prediction_type,
                Prediction.target_datetime == target_datetime,
            )
            .order_by(Prediction.created_at.desc())
            .first()
        )
        if existing is not None:
            return existing
        if attempt < 2:
            time.sleep(0.05)

    return _save_prediction(
        db,
        station_id=station_id,
        prediction_type=prediction_type,
        predicted_value=predicted_value,
        confidence=confidence,
        target_datetime=target_datetime,
        model_version=model_version,
        commit=commit,
    )

def _require_station(db: Session, station_id: int) -> Station:
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    return station

def forecast_crowd(db: Session, station_id: int, target_datetime: datetime | None = None) -> Prediction:
    _require_station(db, station_id)
    dt = target_datetime or datetime.now(timezone.utc)
    result = _cached_predict_crowd(station_id, dt)
    record = _get_or_save_prediction(
        db,
        station_id=station_id,
        prediction_type=PredictionType.CROWD,
        predicted_value=result["predicted_count"],
        confidence=result["confidence"],
        target_datetime=result["target_datetime"],
        model_version=result["model_version"],
    )

    record.models = result.get("models", {})
    return record

def forecast_demand(db: Session, station_id: int, hours_ahead: int = 6) -> list[Prediction]:
    _require_station(db, station_id)
    hours_ahead = min(max(hours_ahead or 1, 1), MAX_DEMAND_FORECAST_HOURS_AHEAD)
    now = datetime.now(timezone.utc)
    records = []
    for i in range(1, hours_ahead + 1):
        target = now + timedelta(hours=i)
        result = _cached_predict_crowd(station_id, target)
        record = _get_or_save_prediction(
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
    _require_station(db, station_id)
    dt = datetime.now(timezone.utc)
    result = _cached_predict_delay(db, station_id, dt, train_id=train_id)
    record = _get_or_save_prediction(
        db,
        station_id=station_id,
        prediction_type=PredictionType.DELAY,
        predicted_value=result["predicted_delay_minutes"],
        confidence=None,
        target_datetime=result["target_datetime"],
        model_version=result["model_version"],
    )

    record.models = result.get("models", {})
    return record

def recommend_train_frequency(db: Session, station_id: int, is_peak_hour: bool = False) -> Prediction:
    _require_station(db, station_id)
    target = datetime.now(timezone.utc)
    if is_peak_hour:
        target = to_business_time(target).replace(hour=9)
    result = _cached_recommend_frequency(station_id, target)
    record = _get_or_save_prediction(
        db,
        station_id=station_id,
        prediction_type=PredictionType.FREQUENCY,
        predicted_value=result["recommended_frequency_minutes"],
        confidence=None,
        target_datetime=result["target_datetime"],
        model_version=result["model_version"],
    )

    record.models = result.get("models", {})
    return record

def traffic_pattern_analysis(db: Session, station_id: int) -> dict:
    _require_station(db, station_id)
    now = to_business_time(datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
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
                                                                   
    cache_key = f"predict:traffic-pattern-aggregate:v3:{state or 'all'}"
    cached = cache.get_json(cache_key)
    if cached is not None:
        return cached

    from app.utils.geo import cities_for_state

    cities = cities_for_state(state)
    query = db.query(Station)
    if cities:
        query = query.filter(Station.city.in_(cities))
    stations = query.all()

    now = to_business_time(datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
    training_ids = dataset_station_id_map()
    station_id_pairs = [
        (station.id, training_ids[station.station_code])
        for station in stations
        if station.station_code in training_ids
    ]
    if not station_id_pairs:
        raise HTTPException(
            status_code=503,
            detail="No stations in this city match the crowd model training dataset.",
        )
    hours = list(range(24))

    bulk_results = predict_crowd_bulk(
        [training_id for _, training_id in station_id_pairs], hours, now
    )

    totals = [0] * 24
    for _, training_id in station_id_pairs:
        for hour in hours:
            totals[hour] += bulk_results[(training_id, hour)]["predicted_count"]

    forecast_source = "trained_model"
    if not any(totals):
        dataset_totals = dataset_hourly_passenger_baseline(cities)
        if not any(dataset_totals):
            raise HTTPException(
                status_code=503,
                detail="Neither the trained model nor the passenger dataset has forecast data for this city.",
            )
        totals = dataset_totals
        forecast_source = "dataset_baseline"

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
        "station_count": len(station_id_pairs),
        "forecast_source": forecast_source,
        "hourly_forecast": curve,
        "peak_hour": peak["hour"],
        "peak_predicted_count": peak["predicted_count"],
        "quietest_hour": trough["hour"],
    }
    cache.set_json(cache_key, response, ttl_seconds=PREDICTION_CACHE_TTL_SECONDS)
    return response

RECOMMENDATION_WRITE_DEDUPE_SECONDS = 60

def _maybe_persist_recommendation_predictions(
    db: Session, station_id: int, crowd: dict, delay: dict, frequency: dict,
) -> None:
    dedupe_key = f"predictions:reco-write:{station_id}"
    if not cache.set_nx(dedupe_key, RECOMMENDATION_WRITE_DEDUPE_SECONDS):
        return

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
        confidence=None,
        target_datetime=delay["target_datetime"],
        model_version=delay["model_version"],
        commit=False,
    )
    _save_prediction(
        db,
        station_id=station_id,
        prediction_type=PredictionType.FREQUENCY,
        predicted_value=frequency["recommended_frequency_minutes"],
        confidence=None,
        target_datetime=frequency["target_datetime"],
        model_version=frequency["model_version"],
        commit=False,
    )
    db.commit()

HIGH_CROWD_THROUGHPUT_RATIO = 0.35

def _build_recommendations(
    station_id: int, crowd: dict, delay: dict, frequency: dict, capacity: int | None = None,
) -> list[dict]:
    recommendations = []
    effective_capacity = capacity or DEFAULT_CAPACITY_FALLBACK
    crowd_ratio = crowd["predicted_count"] / effective_capacity if effective_capacity else 0

    if crowd_ratio > HIGH_CROWD_THROUGHPUT_RATIO:
        recommendations.append({
            "station_id": station_id,
            "title": "High crowd expected",
            "detail": f"Predicted ~{crowd['predicted_count']} passengers "
                      f"({crowd_ratio * 100:.0f}% of this station's capacity). "
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

def smart_recommendations(db: Session, station_id: int) -> list[dict]:
    station = _require_station(db, station_id)
    dt = datetime.now(timezone.utc)
    crowd = _cached_predict_crowd(station_id, dt)
    delay = _cached_predict_delay(db, station_id, dt)
    frequency = _cached_recommend_frequency(station_id, dt)

    _maybe_persist_recommendation_predictions(db, station_id, crowd, delay, frequency)

    return _build_recommendations(station_id, crowd, delay, frequency, capacity=station.capacity)

def smart_recommendations_bulk(db: Session, station_ids: list[int]) -> dict[int, list[dict]]:
    dt = datetime.now(timezone.utc)

    capacities = dict(
        db.query(Station.id, Station.capacity)
        .filter(Station.id.in_(station_ids))
        .all()
    )
    results: dict[int, list[dict]] = {}
    for station_id in station_ids:
        crowd = _cached_predict_crowd(station_id, dt)
        delay = _cached_predict_delay(db, station_id, dt)
        frequency = _cached_recommend_frequency(station_id, dt)

        _maybe_persist_recommendation_predictions(db, station_id, crowd, delay, frequency)

        results[station_id] = _build_recommendations(
            station_id, crowd, delay, frequency, capacity=capacities.get(station_id),
        )
    return results

def get_crowd_model_metrics() -> dict:
    from app.ai_engine.prediction.crowd_metrics import compute_crowd_metrics

    return compute_crowd_metrics()

def get_delay_model_metrics() -> dict:
    from app.ai_engine.prediction.delay_metrics import compute_delay_metrics

    return compute_delay_metrics()

def get_frequency_model_metrics() -> dict:
    from app.ai_engine.prediction.frequency_metrics import compute_frequency_metrics

    return compute_frequency_metrics()

def get_sensor_validated_crowd_metrics(db: Session, lookback_days: int = 30) -> dict:
    now = datetime.now(timezone.utc)
    tolerance = timedelta(minutes=10)
    observed_count = (
        db.query(SensorFeedEvent.observed_count)
        .filter(
            SensorFeedEvent.station_id == Prediction.station_id,
            SensorFeedEvent.observed_count.is_not(None),
            SensorFeedEvent.observed_at >= Prediction.target_datetime - tolerance,
            SensorFeedEvent.observed_at <= Prediction.target_datetime + tolerance,
        )
        .order_by(func.abs(func.extract("epoch", SensorFeedEvent.observed_at - Prediction.target_datetime)))
        .limit(1)
        .scalar_subquery()
    )
    rows = (
        db.query(Prediction.predicted_value, observed_count.label("observed_count"))
        .filter(
            Prediction.prediction_type == PredictionType.CROWD,
            Prediction.target_datetime.is_not(None),
            Prediction.target_datetime >= now - timedelta(days=min(max(lookback_days, 1), 90)),
            Prediction.target_datetime <= now,
            Prediction.created_at <= Prediction.target_datetime,
        )
        .order_by(Prediction.target_datetime.desc())
        .limit(5000)
        .all()
    )
    matched = [(float(predicted), int(actual)) for predicted, actual in rows if actual is not None]
    minimum_samples = 10
    if not matched:
        return {
            "available": False,
            "status": "awaiting_external_observations",
            "source": "authenticated sensor or ticket feed",
            "lookback_days": lookback_days,
            "matched_samples": 0,
            "minimum_samples": minimum_samples,
            "mae": None,
            "mape_pct": None,
        }
    errors = [abs(predicted - actual) for predicted, actual in matched]
    percentage_errors = [error / max(abs(actual), 1) * 100 for error, (_, actual) in zip(errors, matched)]
    return {
        "available": len(matched) >= minimum_samples,
        "status": "validated" if len(matched) >= minimum_samples else "insufficient_external_observations",
        "source": "authenticated sensor or ticket feed",
        "lookback_days": lookback_days,
        "matched_samples": len(matched),
        "minimum_samples": minimum_samples,
        "mae": round(sum(errors) / len(errors), 3),
        "mape_pct": round(sum(percentage_errors) / len(percentage_errors), 3),
    }
