"""Milestone 2 - AI Prediction Module: crowd prediction inference.

Loads the trained RandomForest model if present; otherwise falls back
to a deterministic heuristic curve so the API keeps working even
before `train_crowd_model.py` has been run (e.g. fresh deploy).

Milestone 17 - scikit-learn compatibility: `joblib.load()` succeeding
does NOT guarantee `model.predict()` will also succeed. scikit-learn's
own version check (the pickle's embedded `_sklearn_version`) only ever
emits a warning, never raises - so a genuinely incompatible model
(trained on a different sklearn than what's currently installed, e.g.
these .pkl files were trained on 1.6.1) can load "fine" and then blow
up with an AttributeError/ValueError deep inside `.predict()` on the
very first real request, at which point the load-time try/except below
has already reported success and can't help. Every `.predict()` call
in this module is wrapped separately (see `predict_crowd`) so a
predict-time failure degrades that one request to the heuristic
instead of surfacing a 500.
"""
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

def _predict_one(model, model_name: str, features: pd.DataFrame, light: bool) -> dict:
    """Run a single already-fitted model and shape its output the same
    way regardless of which algorithm it is."""
    # Passenger counts can never be negative - a regression model can
    # still extrapolate below zero for feature combos it saw little of
    # during training (e.g. very late-night hours), so clamp here the
    # same way delay_predictor/frequency_predictor already clamp their
    # own outputs, instead of letting a negative count leak into the
    # dashboards (aggregate sums of many negative per-station
    # predictions is what made "Passenger Analytics" render upside
    # down with negative totals).
    predicted_count = max(0.0, float(model.predict(features)[0]))
    if light:
        confidence = None
    elif hasattr(model, "estimators_"):
        # Per-tree confidence only makes sense for a forest
        # (RandomForest); XGBoost's boosted trees aren't independent
        # votes, so there's no equivalent cheap per-tree spread here.
        tree_preds = [t.predict(features.values)[0] for t in model.estimators_]
        confidence = float(max(0.0, 1 - (np.std(tree_preds) / (np.mean(tree_preds) + 1e-6))))
    else:
        confidence = None
    return {
        "predicted_count": round(predicted_count),
        "confidence": None if confidence is None else round(min(confidence, 0.99), 3),
        "model_version": f"{model_name}_v1",
    }

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
    actually sees confidence) still calls this with light=False.

    Returns the winning (lowest-MAE-at-training-time) model's result at
    the top level, same shape as before, PLUS a `models` dict with every
    trained candidate's prediction (currently random_forest and
    xgboost) so callers that want to show both side by side can, without
    the winner-takes-all fallback logic needing to change."""
    dt = target_datetime or datetime.utcnow()
    hour = dt.hour
    day_of_week = dt.weekday()
    is_weekend = 1 if day_of_week in (5, 6) else 0
    is_peak_hour = 1 if (8 <= hour <= 11 or 17 <= hour <= 20) else 0

    bundle = _load_model()
    predicted_count = confidence = model_version = None
    per_model: dict[str, dict] = {}

    if bundle is not None:
        try:
            features = pd.DataFrame(
                [[station_id, hour, day_of_week, is_weekend, is_peak_hour]],
                columns=bundle["features"],
            )
            trained_name = bundle.get("model_name", "random_forest")
            all_candidates = bundle.get("models") or {trained_name: bundle["model"]}

            if light:
                # Simulator hot path (called once per station every
                # tick, never reads `models`) - only run the winning
                # model, exactly like before this file supported dual
                # predictions. Running both candidates here would
                # silently double inference cost on every tick for a
                # result nobody consumes.
                winner_model = all_candidates.get(trained_name) or next(iter(all_candidates.values()))
                winner = _predict_one(winner_model, trained_name, features, light)
            else:
                # Real user-facing prediction call - compute every
                # candidate so the API/dashboard can show Random Forest
                # and XGBoost side by side.
                for name, model in all_candidates.items():
                    per_model[name] = _predict_one(model, name, features, light)
                winner = per_model.get(trained_name) or next(iter(per_model.values()))

            predicted_count = winner["predicted_count"]
            confidence = winner["confidence"]
            model_version = winner["model_version"]
        except Exception as exc:                                                  
                                                                    
            logger.warning(
                "crowd model .predict() failed (%r) - using heuristic fallback for this request",
                exc,
            )
            predicted_count = None
            per_model = {}

    if predicted_count is None:
        predicted_count = round(_heuristic(hour, is_weekend))
        confidence = None if light else 0.5
        model_version = "heuristic_fallback"
        per_model = {}

    return {
        "station_id": station_id,
        "target_datetime": dt,
        "predicted_count": predicted_count,
        "confidence": confidence,
        "model_version": model_version,
        "models": per_model,
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

    trained_name = bundle.get("model_name", "random_forest") if bundle is not None else None
    for i, (station_id, hour) in enumerate(keys):
        if predicted is not None:
            # Same non-negative clamp as _predict_one() above - this is
            # the vectorized path all_stations_traffic_pattern() calls
            # for every station x hour, so an unclamped negative value
            # here is what was summing into a large negative total per
            # hour on the "Passenger Analytics" (all stations) chart.
            predicted_count = max(0.0, float(predicted[i]))
            model_version = f"{trained_name}_v1"
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
