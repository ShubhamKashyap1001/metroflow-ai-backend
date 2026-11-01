
import logging
import os
import time as _time
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy.orm import Session

from app.ai_engine import model_bundle
from app.ai_engine.prediction.crowd_predictor import predict_crowd
from app.core import cache
from app.models.train import Train
from app.utils.timezone import business_today, to_business_time

logger = logging.getLogger(__name__)

MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "saved_models", "delay_model.pkl")

KNOWN_FEATURES = {
    "station_id", "station_code", "hour", "day_of_week", "is_weekend",
    "is_peak_hour", "passenger_count", "capacity_passengers",
    "train_age_days", "train_age_years", "weather_code",
}

def _load_model():

    return model_bundle.get_or_load("delay", MODEL_PATH)

FLEET_STATS_CACHE_TTL_SECONDS = 300
_FLEET_STATS_CACHE_KEY = "predict:delay:fleet-stats"
_fleet_stats_local: tuple[float, float] | None = None
_fleet_stats_local_at: float = 0.0

def _compute_fleet_stats(db: Session) -> tuple[float, float]:
    rows = (
        db.query(Train.capacity, Train.commissioned_date)
        .filter(Train.is_active.is_(True))
        .all()
    )
    if not rows:
        return 1200.0, 0.0

    capacities = [c for c, _ in rows if c is not None]
    avg_capacity = sum(capacities) / len(capacities) if capacities else 1200.0

    today = business_today()
    ages = [(today - commissioned).days for _, commissioned in rows if commissioned is not None]
    avg_age_days = sum(ages) / len(ages) if ages else 0.0

    return avg_capacity, avg_age_days

def _fleet_stats(db: Session) -> tuple[float, float]:
    global _fleet_stats_local, _fleet_stats_local_at

    if _fleet_stats_local is not None and (_time.monotonic() - _fleet_stats_local_at) < FLEET_STATS_CACHE_TTL_SECONDS:
        return _fleet_stats_local

    cached = cache.get_json(_FLEET_STATS_CACHE_KEY)
    if cached is not None:
        stats = (cached["avg_capacity"], cached["avg_age_days"])
        _fleet_stats_local = stats
        _fleet_stats_local_at = _time.monotonic()
        return stats

    stats = _compute_fleet_stats(db)
    _fleet_stats_local = stats
    _fleet_stats_local_at = _time.monotonic()
    cache.set_json(
        _FLEET_STATS_CACHE_KEY,
        {"avg_capacity": stats[0], "avg_age_days": stats[1]},
        ttl_seconds=FLEET_STATS_CACHE_TTL_SECONDS,
    )
    return stats

def invalidate_fleet_stats_cache() -> None:
    global _fleet_stats_local, _fleet_stats_local_at
    cache.delete(_FLEET_STATS_CACHE_KEY)
    _fleet_stats_local = None
    _fleet_stats_local_at = 0.0

def _real_capacity_passengers(db: Session | None, train_id: int | None) -> float:
    if db is None:
        return 1200.0                                                  
    if train_id is not None:
        train = db.get(Train, train_id)
        if train is not None:
            return float(train.capacity)
    avg_capacity, _ = _fleet_stats(db)
    return avg_capacity

def _real_train_age_days(db: Session | None, train_id: int | None) -> float:
    if db is None:
        return 0.0

    if train_id is not None:
        train = db.get(Train, train_id)
        if train is not None and train.commissioned_date is not None:
            return float((business_today() - train.commissioned_date).days)

    _, avg_age_days = _fleet_stats(db)
    return avg_age_days

def predict_delay(
    station_id: int,
    target_datetime: datetime | None = None,
    train_id: int | None = None,
    db: Session | None = None,
) -> dict:
    dt = target_datetime or datetime.now(timezone.utc)
    local_dt = to_business_time(dt)
    hour = local_dt.hour
    day_of_week = local_dt.weekday()
    is_weekend = 1 if day_of_week in (5, 6) else 0
    is_peak_hour = 1 if (8 <= hour <= 11 or 17 <= hour <= 20) else 0

    crowd = predict_crowd(station_id, dt)
    passenger_count = crowd["predicted_count"]

    bundle = _load_model()
    predicted_delay = None
    per_model: dict[str, dict] = {}

    if bundle is not None:
        try:
            model_bundle.validate_feature_contract(
                bundle["features"], KNOWN_FEATURES, context="delay_predictor"
            )
            trained_name = bundle.get("model_name", "random_forest")
            candidates = bundle.get("models") or {trained_name: bundle["model"]}
            train_age_days = _real_train_age_days(db, train_id)

            row_values = {
                "station_id": station_id,
                "station_code": station_id,
                "hour": hour,
                "day_of_week": day_of_week,
                "is_weekend": is_weekend,
                "is_peak_hour": is_peak_hour,
                "passenger_count": passenger_count,
                "capacity_passengers": _real_capacity_passengers(db, train_id),
                "train_age_days": train_age_days,
                "train_age_years": train_age_days / 365.25,

                "weather_code": 0,
            }
                                                                       
            features = pd.DataFrame(
                [[row_values[f] for f in bundle["features"]]],
                columns=bundle["features"],
            )
            for name, model in candidates.items():
                per_model[name] = {
                    "predicted_delay_minutes": round(max(0.0, float(model.predict(features)[0])), 1),
                    "model_version": f"{name}_v1",
                }
            winner = per_model.get(trained_name) or next(iter(per_model.values()))
            predicted_delay = winner["predicted_delay_minutes"]
            model_version = winner["model_version"]
        except Exception as exc:                                                  
                                                                     
            logger.warning(
                "delay model .predict() failed (%r) - using heuristic fallback for this request",
                exc,
            )
            predicted_delay = None
            per_model = {}

    if predicted_delay is None:
        predicted_delay = round(max(0.0, (passenger_count / 1000) * 4), 1)
        model_version = "heuristic_fallback"
        per_model = {}

    return {
        "station_id": station_id,
        "target_datetime": dt,
        "predicted_delay_minutes": predicted_delay,
        "based_on_predicted_crowd": passenger_count,
        "model_version": model_version,

        "models": per_model,
    }
