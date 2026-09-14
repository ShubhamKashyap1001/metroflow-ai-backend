"""Milestone 2 - AI Prediction Module: train frequency recommendation
inference. Supports the Scheduling Management Module's "frequency
adjustment" workflow with a data-driven suggestion.
"""
import logging
import os
from datetime import datetime, timezone

import pandas as pd

from app.ai_engine import model_bundle
from app.utils.timezone import to_business_time

logger = logging.getLogger(__name__)

MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "saved_models", "frequency_model.pkl")

MIN_FREQUENCY = 3
MAX_FREQUENCY = 15

# Every raw input this predictor can actually compute - see
# recommend_frequency() below. Used to validate a loaded bundle's
# `features` list before trusting its column order (see
# model_bundle.validate_feature_contract).
KNOWN_FEATURES = {"station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour"}

def _load_model():
    # See crowd_predictor._load_model - same shared, thread-safe,
    # load-once registry (app.ai_engine.model_bundle.get_or_load).
    return model_bundle.get_or_load("frequency", MODEL_PATH)

def recommend_frequency(station_id: int, target_datetime: datetime | None = None) -> dict:
    dt = target_datetime or datetime.now(timezone.utc)
    # BUGFIX (naive datetime / timezone handling): same fix as
    # crowd_predictor.predict_crowd / delay_predictor.predict_delay -
    # peak-hour/weekend features are business-local concepts, derived
    # from `dt` converted into the app's configured business timezone
    # rather than `dt`'s own tzinfo. `dt` itself is unchanged. See
    # app/utils/timezone.py.
    local_dt = to_business_time(dt)
    hour = local_dt.hour
    day_of_week = local_dt.weekday()
    is_weekend = 1 if day_of_week in (5, 6) else 0
    is_peak_hour = 1 if (8 <= hour <= 11 or 17 <= hour <= 20) else 0

    bundle = _load_model()
    recommended = None
    per_model: dict[str, dict] = {}

    if bundle is not None:
        try:
            model_bundle.validate_feature_contract(
                bundle["features"], KNOWN_FEATURES, context="frequency_predictor"
            )
            trained_name = bundle.get("model_name", "random_forest")
            candidates = bundle.get("models") or {trained_name: bundle["model"]}
            features = pd.DataFrame(
                [[station_id, hour, day_of_week, is_weekend, is_peak_hour]],
                columns=bundle["features"],
            )
            for name, model in candidates.items():
                raw = float(model.predict(features)[0])
                clamped = max(MIN_FREQUENCY, min(MAX_FREQUENCY, raw))
                per_model[name] = {
                    "recommended_frequency_minutes": round(clamped, 1),
                    "model_version": f"{name}_v1",
                }
            winner = per_model.get(trained_name) or next(iter(per_model.values()))
            recommended = winner["recommended_frequency_minutes"]
            model_version = winner["model_version"]
        except Exception as exc:                                                  
            logger.warning(
                "frequency model .predict() failed (%r) - using heuristic fallback for this request",
                exc,
            )
            recommended = None
            per_model = {}

    if recommended is None:
                                                       
        recommended = MIN_FREQUENCY + 2 if is_peak_hour else MAX_FREQUENCY - 2
        recommended = max(MIN_FREQUENCY, min(MAX_FREQUENCY, recommended))
        model_version = "heuristic_fallback"
        per_model = {}

    return {
        "station_id": station_id,
        "target_datetime": dt,
        "is_peak_hour": bool(is_peak_hour),
        "recommended_frequency_minutes": round(recommended, 1),
        "model_version": model_version,
        # Per-candidate breakdown (random_forest / xgboost), same
        # pattern as crowd_predictor.predict_crowd's `models` field.
        "models": per_model,
    }
