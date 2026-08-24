import logging
import os
from datetime import datetime
from functools import lru_cache

import joblib
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "saved_models", "crowd_model.pkl")

@lru_cache(maxsize=1)
def _load_model():
    if not os.path.exists(MODEL_PATH):
        print(f"[{__name__}] no trained model at {MODEL_PATH} - using heuristic fallback")
        return None
    try:
        return joblib.load(MODEL_PATH)
    except Exception as exc:
                                                                     
        print(f"[{__name__}] failed to load {MODEL_PATH}: {exc!r} - using heuristic fallback")
        return None

def _heuristic(hour: int, is_weekend: int) -> float:
    morning_peak = np.exp(-((hour - 9) ** 2) / 4) * 900
    evening_peak = np.exp(-((hour - 18.5) ** 2) / 5) * 950
    base = 150 + morning_peak + evening_peak
    if is_weekend:
        base *= 0.55
    return float(base)

def predict_crowd(
    station_id: int,
    target_datetime: datetime | None = None,
    light: bool = False,
) -> dict:
    """light=True skips the per-tree confidence pass (walking every
    tree in the RandomForest separately) and only returns
    predicted_count. Used by the live simulator, which calls this once
    per station every tick just to *weight* random check-ins and never
    reads `confidence` at all - running the full forest-vote loop
    there was pure wasted CPU that added up across every station on
    every tick. The real /prediction/crowd API endpoint (where a user
    actually sees confidence) still calls this with light=False."""
    dt = target_datetime or datetime.utcnow()
    hour = dt.hour
    day_of_week = dt.weekday()
    is_weekend = 1 if day_of_week in (5, 6) else 0
    is_peak_hour = 1 if (8 <= hour <= 11 or 17 <= hour <= 20) else 0

    bundle = _load_model()
    predicted_count = confidence = model_version = None

    if bundle is not None:
        try:
            model = bundle["model"]
            features = pd.DataFrame(
                [[station_id, hour, day_of_week, is_weekend, is_peak_hour]],
                columns=bundle["features"],
            )
            predicted_count = float(model.predict(features)[0])
            if light:
                confidence = None
            else:
                                                                            
                tree_preds = [t.predict(features.values)[0] for t in model.estimators_]
                confidence = float(max(0.0, 1 - (np.std(tree_preds) / (np.mean(tree_preds) + 1e-6))))
            model_version = "random_forest_v1"
        except Exception as exc:                                                  
                                                                    
            logger.warning(
                "crowd model .predict() failed (%r) - using heuristic fallback for this request",
                exc,
            )
            predicted_count = None

    if predicted_count is None:
        predicted_count = _heuristic(hour, is_weekend)
        confidence = None if light else 0.5
        model_version = "heuristic_fallback"

    return {
        "station_id": station_id,
        "target_datetime": dt,
        "predicted_count": round(predicted_count),
        "confidence": None if confidence is None else round(min(confidence, 0.99), 3),
        "model_version": model_version,
    }

def predict_crowd_bulk(station_ids: list[int], hours: list[int], target_date: datetime) -> dict[tuple[int, int], dict]:
    """Vectorized version of predict_crowd(..., light=True) for many
    (station_id, hour) pairs at once - one DataFrame, one model.predict()
    call, instead of one Python-level call (and one pandas DataFrame
    construction) per pair.

    Built for all_stations_traffic_pattern(), which previously called
    predict_crowd() individually for every station x every one of 24
    hours in a plain Python for-loop - for a city with, say, 40
    stations that's 960 separate model invocations built and run one
    at a time on every cache-cold request, which is what made the
    "Passenger Analytics" widget both slow to load and prone to timing
    out entirely under load. scikit-learn's model.predict() is already
    vectorized internally - it costs barely more to score 1000 rows in
    one call than 10 - so batching turns 960 sequential Python-level
    calls into 1.

    Returns a dict keyed by (station_id, hour) with the same
    predicted_count/confidence=None/model_version shape as an
    individual light=True predict_crowd() call, so callers can swap
    the loop for a single bulk call without changing anything
    downstream."""
    day_of_week = target_date.weekday()
    is_weekend = 1 if day_of_week in (5, 6) else 0

    bundle = _load_model()
    results: dict[tuple[int, int], dict] = {}

    rows = []
    keys = []
    for station_id in station_ids:
        for hour in hours:
            rows.append([station_id, hour, day_of_week, is_weekend,
                         1 if (8 <= hour <= 11 or 17 <= hour <= 20) else 0])
            keys.append((station_id, hour))

    predicted = None
    if bundle is not None and rows:
        try:
            model = bundle["model"]
            features = pd.DataFrame(rows, columns=bundle["features"])
            predicted = model.predict(features)
        except Exception as exc:
            logger.warning(
                "crowd model bulk .predict() failed (%r) - using heuristic fallback for this batch",
                exc,
            )
            predicted = None

    for i, (station_id, hour) in enumerate(keys):
        if predicted is not None:
            predicted_count = float(predicted[i])
            model_version = "random_forest_v1"
        else:
            predicted_count = _heuristic(hour, is_weekend)
            model_version = "heuristic_fallback"
        dt = target_date.replace(hour=hour, minute=0, second=0, microsecond=0)
        results[(station_id, hour)] = {
            "station_id": station_id,
            "target_datetime": dt,
            "predicted_count": round(predicted_count),
            "confidence": None,
            "model_version": model_version,
        }

    return results
