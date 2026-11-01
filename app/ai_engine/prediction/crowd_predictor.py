import csv
import gzip
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache

import numpy as np
import pandas as pd

from app.ai_engine import model_bundle
from app.utils.timezone import to_business_time

logger = logging.getLogger(__name__)

MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "saved_models", "crowd_model.pkl")
DATASET_STATIONS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "datasets", "source", "stations.csv.gz"
)

KNOWN_FEATURES = {"station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour"}

@lru_cache(maxsize=1)
def dataset_station_id_map() -> dict[str, int]:
    required = ("station_id", "city", "line", "station_name", "latitude", "longitude")
    mapping: dict[str, int] = {}
    try:
        with gzip.open(DATASET_STATIONS_PATH, "rt", newline="", encoding="utf-8") as source:
            for row in csv.DictReader(source):
                code = (row.get("station_id") or "").strip()
                if not code or code in mapping:
                    continue
                if any(not (row.get(column) or "").strip() for column in required):
                    continue
                mapping[code] = len(mapping) + 1
    except OSError as exc:
        logger.warning("Could not load dataset station IDs from %s: %s", DATASET_STATIONS_PATH, exc)
    return mapping

@lru_cache(maxsize=1)
def _dataset_hourly_city_totals() -> dict[str, tuple[int, ...]]:
    flow_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "datasets", "source", "passenger_flow.csv.gz"
    )
    daily_totals: dict[tuple[str, str, int], int] = defaultdict(int)
    try:
        with gzip.open(flow_path, "rt", newline="", encoding="utf-8") as source:
            for row in csv.DictReader(source):
                city = (row.get("city") or "").strip()
                timestamp = (row.get("timestamp") or "").strip()
                try:
                    hour = int(row.get("hour", ""))
                    passengers = max(0, int(float(row.get("entries", "0")))) + max(
                        0, int(float(row.get("exits", "0")))
                    )
                except (TypeError, ValueError):
                    continue
                day = timestamp[:10]
                if city and len(day) == 10 and 0 <= hour <= 23:
                    daily_totals[(city, day, hour)] += passengers
    except OSError as exc:
        logger.warning("Could not load passenger dataset baseline from %s: %s", flow_path, exc)
        return {}

    sums: dict[tuple[str, int], int] = defaultdict(int)
    day_counts: dict[tuple[str, int], int] = defaultdict(int)
    for (city, _day, hour), total in daily_totals.items():
        sums[(city, hour)] += total
        day_counts[(city, hour)] += 1

    return {
        city: tuple(
            round(sums[(city, hour)] / day_counts[(city, hour)])
            if day_counts[(city, hour)]
            else 0
            for hour in range(24)
        )
        for city, _hour in sums
    }

def dataset_hourly_passenger_baseline(cities: list[str] | None) -> list[int]:
    profiles = _dataset_hourly_city_totals()
    selected_cities = cities or list(profiles)
    return [
        sum(profiles.get(city, (0,) * 24)[hour] for city in selected_cities)
        for hour in range(24)
    ]

def _load_model():

    return model_bundle.get_or_load("crowd", MODEL_PATH)

def _heuristic(hour: int, is_weekend: int) -> float:
    morning_peak = np.exp(-((hour - 9) ** 2) / 4) * 900
    evening_peak = np.exp(-((hour - 18.5) ** 2) / 5) * 950
    base = 150 + morning_peak + evening_peak
    if is_weekend:
        base *= 0.55
    return float(base)

def _predict_one(model, model_name: str, features: pd.DataFrame, light: bool) -> dict:

    predicted_count = max(0.0, float(model.predict(features)[0]))
    if light:
        confidence = None
    elif hasattr(model, "estimators_"):

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
    dt = target_datetime or datetime.now(timezone.utc)
    local_dt = to_business_time(dt)
    hour = local_dt.hour
    day_of_week = local_dt.weekday()
    is_weekend = 1 if day_of_week in (5, 6) else 0
    is_peak_hour = 1 if (8 <= hour <= 11 or 17 <= hour <= 20) else 0

    bundle = _load_model()
    predicted_count = confidence = model_version = None
    per_model: dict[str, dict] = {}

    if bundle is not None:
        try:
            model_bundle.validate_feature_contract(
                bundle["features"], KNOWN_FEATURES, context="crowd_predictor"
            )
            features = pd.DataFrame(
                [[station_id, hour, day_of_week, is_weekend, is_peak_hour]],
                columns=bundle["features"],
            )
            trained_name = bundle.get("model_name", "random_forest")
            all_candidates = bundle.get("models") or {trained_name: bundle["model"]}

            if light:

                winner_model = all_candidates.get(trained_name) or next(iter(all_candidates.values()))
                winner = _predict_one(winner_model, trained_name, features, light)
            else:

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

        confidence = None
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
            model_bundle.validate_feature_contract(
                bundle["features"], KNOWN_FEATURES, context="crowd_predictor.bulk"
            )
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
