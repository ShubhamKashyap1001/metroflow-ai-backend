
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

# Every raw input name this predictor knows how to compute a value for
# (see the `row_values` mapping built in predict_delay() below - this
# is exactly its key set). Used to validate a loaded bundle's
# `features` list before trusting its column order (see
# model_bundle.validate_feature_contract).
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
    """Single query for both capacity and commissioned_date across
    every active train (was two separate `.all()` queries before this
    fix), returning (avg_capacity, avg_age_days). Falls back to the
    same defaults the old per-metric functions used if there's no
    active-train data at all."""
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
    """Drop the cached fleet-wide (avg_capacity, avg_age_days) pair.

    Called after a train is created/updated (see
    train_service.create_train/update_train) so a capacity/
    commissioned_date change - or a brand new train - is reflected in
    the next delay prediction that falls back to the fleet average,
    instead of waiting out the rest of FLEET_STATS_CACHE_TTL_SECONDS.
    Same "clear both layers" shape as train_tracking.py's
    invalidate_route_cache()."""
    global _fleet_stats_local, _fleet_stats_local_at
    cache.delete(_FLEET_STATS_CACHE_KEY)
    _fleet_stats_local = None
    _fleet_stats_local_at = 0.0

def _real_capacity_passengers(db: Session | None, train_id: int | None) -> float:
    """Real Train.capacity - this train's own if we know which train,
    otherwise the real average across active trains (cached - see
    _fleet_stats above). Never a made-up constant."""
    if db is None:
        return 1200.0                                                  
    if train_id is not None:
        train = db.get(Train, train_id)
        if train is not None:
            return float(train.capacity)
    avg_capacity, _ = _fleet_stats(db)
    return avg_capacity

def _real_train_age_days(db: Session | None, train_id: int | None) -> float:
    """Real (today - Train.commissioned_date).days for this train, or
    the real average age across active trains (cached - see
    _fleet_stats above) if no specific train_id is given. 0.0 only if
    there's genuinely no commissioned_date data at all (e.g. an older
    synthetic seed)."""
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
    # BUGFIX (naive datetime / timezone handling): same fix as
    # crowd_predictor.predict_crowd - peak-hour/weekend features are
    # business-local concepts, so they're derived from `dt` converted
    # into the app's configured business timezone, not from `dt`'s own
    # (usually UTC) tzinfo. `dt` itself is unchanged. See
    # app/utils/timezone.py.
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
            # row_values carries every name/unit this bundle's "features"
            # list might use, since different training runs of this
            # model have shipped with different naming: some use
            # "station_id" / "train_age_days", others (e.g. the current
            # real-data crowd/delay/frequency .pkl set) use "station_code"
            # / "train_age_years". A plain rename would silently corrupt
            # the delay prediction for the *_years case (years and days
            # are on completely different scales), so it's converted
            # here, not just aliased.
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
                # No live weather feed wired up yet, so this defaults to
                # 0 ("Sunny" in the training encoding - Sunny/Overcast/
                # Rainy/Stormy = 0/1/2/3), the single most common
                # category in the training data (~56% of rows). Once a
                # real weather API/DB column is wired up at inference
                # time, replace this constant with that live value.
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
        # Per-candidate breakdown (random_forest / xgboost), same
        # pattern as crowd_predictor.predict_crowd's `models` field.
        "models": per_model,
    }
