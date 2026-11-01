import json
import os
from functools import lru_cache

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import train_test_split

from app.utils.timezone import business_today

MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "saved_models", "delay_model.pkl")

METRICS_JSON_PATH = os.path.join(os.path.dirname(__file__), "..", "saved_models", "delay_model_metrics.json")
DATASET_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "datasets", "source")
STATIONS_CSV = os.path.join(DATASET_DIR, "stations.csv.gz")
PASSENGER_FLOW_CSV = os.path.join(DATASET_DIR, "passenger_flow.csv.gz")
TRAIN_OPERATIONS_CSV = os.path.join(DATASET_DIR, "train_operations.csv.gz")
TRAINS_CSV = os.path.join(DATASET_DIR, "trains.csv.gz")

PASSENGER_FLOW_USECOLS = ["station_id", "hour", "day_of_week", "is_weekend", "entries", "exits"]
TRAIN_OPERATIONS_USECOLS = [
    "station_id", "train_id", "scheduled_arrival", "delay_arrival_min", "weather",
]
CSV_CHUNK_SIZE = 100_000

FEATURES = ["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour",
            "passenger_count", "capacity_passengers", "train_age_days", "weather_code"]
TARGET = "delay_minutes"

DISPLAY_NAMES = {
    "random_forest": "Random Forest",
    "xgboost": "XGBoost",

    "random_forest_tuned": "Random Forest",
    "xgboost_tuned": "XGBoost",
}

WEATHER_CODE = {"Sunny": 0, "Overcast": 1, "Rainy": 2, "Stormy": 3}

def _station_id_map() -> dict[str, int]:
    stations = pd.read_csv(STATIONS_CSV)
    for col in ["station_id", "city", "line", "station_name"]:
        stations[col] = stations[col].astype(str).str.strip()
    stations = stations.drop_duplicates(subset=["station_id"]).dropna(
        subset=["station_id", "city", "line", "station_name", "latitude", "longitude"]
    ).reset_index(drop=True)
    stations["int_station_id"] = stations.index + 1
    return dict(zip(stations["station_id"], stations["int_station_id"]))

def _train_info_map() -> pd.DataFrame:
    trains = pd.read_csv(TRAINS_CSV)
    trains["train_id"] = trains["train_id"].astype(str).str.strip()
    trains["commissioned_date"] = pd.to_datetime(trains["commissioned_date"])
    trains = trains.reset_index(drop=True)
    today = pd.Timestamp(business_today())
    trains["train_age_days"] = (today - trains["commissioned_date"]).dt.days.astype(float)
    return trains[["train_id", "capacity_passengers", "train_age_days"]]

def _crowd_table(station_id_map: dict) -> pd.DataFrame:
    partial_group_sums: list[pd.DataFrame] = []
    group_keys = ["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour"]

    for chunk in pd.read_csv(PASSENGER_FLOW_CSV, usecols=PASSENGER_FLOW_USECOLS, chunksize=CSV_CHUNK_SIZE):
        chunk = chunk.copy()
        chunk["station_id"] = chunk["station_id"].astype(str).str.strip().map(station_id_map)
        chunk = chunk.dropna(subset=["station_id"])
        chunk["station_id"] = chunk["station_id"].astype(int)

        chunk["entries"] = chunk["entries"].clip(lower=0)
        chunk["exits"] = chunk["exits"].clip(lower=0)
        chunk["passenger_count"] = chunk["entries"] + chunk["exits"]
        chunk["is_peak_hour"] = ((chunk["hour"].between(8, 11)) | (chunk["hour"].between(17, 20))).astype(int)

        chunk_grouped = chunk.groupby(group_keys)["passenger_count"].agg(["sum", "count"]).reset_index()
        partial_group_sums.append(chunk_grouped)

    if not partial_group_sums:
        return pd.DataFrame(columns=group_keys + ["passenger_count"])

    combined = pd.concat(partial_group_sums, ignore_index=True)
    combined = combined.groupby(group_keys)[["sum", "count"]].sum().reset_index()
    combined["passenger_count"] = (combined["sum"] / combined["count"]).round().astype(int)
    return combined[group_keys + ["passenger_count"]].sort_values(group_keys).reset_index(drop=True)

def _delay_table(station_id_map: dict, train_info: pd.DataFrame) -> pd.DataFrame:
    kept_chunks: list[pd.DataFrame] = []
    total_before = 0

    for chunk in pd.read_csv(TRAIN_OPERATIONS_CSV, usecols=TRAIN_OPERATIONS_USECOLS, chunksize=CSV_CHUNK_SIZE):
        chunk = chunk.copy()
        chunk["station_id"] = chunk["station_id"].astype(str).str.strip().map(station_id_map)
        chunk = chunk.dropna(subset=["station_id"])
        chunk["station_id"] = chunk["station_id"].astype(int)

        chunk["train_id"] = chunk["train_id"].astype(str).str.strip()

        chunk["scheduled_arrival"] = pd.to_datetime(chunk["scheduled_arrival"])
        chunk["hour"] = chunk["scheduled_arrival"].dt.hour
        chunk["day_of_week"] = chunk["scheduled_arrival"].dt.weekday
        chunk["is_weekend"] = (chunk["day_of_week"] >= 5).astype(int)
        chunk["is_peak_hour"] = ((chunk["hour"].between(8, 11)) | (chunk["hour"].between(17, 20))).astype(int)
        chunk["delay_minutes"] = chunk["delay_arrival_min"].fillna(0).clip(lower=0)
        chunk["weather_code"] = chunk["weather"].map(WEATHER_CODE).fillna(0).astype(int)

        chunk = chunk.merge(train_info, on="train_id", how="left")
        total_before += len(chunk)
        chunk = chunk.dropna(subset=["capacity_passengers", "train_age_days"])
        kept_chunks.append(chunk[["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour",
                                   "delay_minutes", "capacity_passengers", "train_age_days", "weather_code"]])

    if kept_chunks:
        df = pd.concat(kept_chunks, ignore_index=True)
    else:
        df = pd.DataFrame(columns=["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour",
                                    "delay_minutes", "capacity_passengers", "train_age_days", "weather_code"])

    dropped = total_before - len(df)
    if dropped:
        print(f"[{__name__}] delay metrics: dropped {dropped} row(s) with unknown train_id")

    return df

def _load_model():
    if not os.path.exists(MODEL_PATH):
        return None
    try:
        return joblib.load(MODEL_PATH)
    except Exception as exc:
        print(f"[{__name__}] failed to load {MODEL_PATH}: {exc!r}")
        return None

def _unavailable() -> dict:
    return {
        "available": False,
        "model_name": None,
        "mae": None,
        "mape_pct": None,
        "r2": None,
        "trained_rows": None,
        "test_rows": None,
        "feature_importance": [],
        "models": {},
    }

def _evaluate_one(model, model_features, X_test, y_test_arr, trained_rows) -> dict:
    predicted = model.predict(X_test[model_features])

    mae = float(mean_absolute_error(y_test_arr, predicted))
    r2 = float(r2_score(y_test_arr, predicted))

    nonzero = y_test_arr > 0
    mape = (
        float(np.mean(np.abs((y_test_arr[nonzero] - predicted[nonzero]) / y_test_arr[nonzero])) * 100)
        if nonzero.any() else None
    )

    importances = getattr(model, "feature_importances_", None)
    if importances is not None:
        feature_importance = sorted(
            (
                {"feature": f, "importance": float(imp)}
                for f, imp in zip(model_features, importances)
            ),
            key=lambda item: item["importance"],
            reverse=True,
        )
    else:
        feature_importance = []

    return {
        "available": True,
        "mae": round(mae, 2),
        "mape_pct": round(mape, 2) if mape is not None else None,
        "r2": round(r2, 4),
        "trained_rows": trained_rows,
        "test_rows": len(X_test),
        "feature_importance": feature_importance,
    }

@lru_cache(maxsize=1)
def compute_delay_metrics() -> dict:
    if not os.path.exists(METRICS_JSON_PATH):
        print(f"[{__name__}] no precomputed metrics at {METRICS_JSON_PATH} - "
              f"run colab_training/precompute_dashboard_metrics.py")
        return _unavailable()
    try:
        with open(METRICS_JSON_PATH) as f:
            return json.load(f)
    except Exception as exc:
        print(f"[{__name__}] failed to read {METRICS_JSON_PATH}: {exc!r}")
        return _unavailable()

def _live_compute_delay_metrics() -> dict:
    bundle = _load_model()
    if bundle is None:
        return _unavailable()

    if not (os.path.exists(STATIONS_CSV) and os.path.exists(PASSENGER_FLOW_CSV)
            and os.path.exists(TRAIN_OPERATIONS_CSV)):
        print(f"[{__name__}] missing dataset files under {DATASET_DIR}")
        return _unavailable()

    try:
        model_features = bundle.get("features", FEATURES)
        trained_name = bundle.get("model_name", "random_forest")
        candidates = bundle.get("models") or {trained_name: bundle["model"]}

        station_id_map = _station_id_map()
        train_info = _train_info_map()
        crowd = _crowd_table(station_id_map)
        delay = _delay_table(station_id_map, train_info)

        merged = delay.merge(
            crowd,
            on=["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour"],
            how="left",
        )

        station_avg = crowd.groupby("station_id")["passenger_count"].mean()
        merged["passenger_count"] = merged["passenger_count"].fillna(
            merged["station_id"].map(station_avg)
        )
        merged["passenger_count"] = merged["passenger_count"].fillna(
            crowd["passenger_count"].mean()
        )
        merged[TARGET] = merged[TARGET].fillna(0.0)

        X = merged[FEATURES]
        y = merged[TARGET]
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
        y_test_arr = y_test.to_numpy()

        models_out = {}
        for name, candidate_model in candidates.items():
            evaluated = _evaluate_one(candidate_model, model_features, X_test, y_test_arr, len(X_train))
            evaluated["model_name"] = DISPLAY_NAMES.get(name, name)
            models_out[name] = evaluated

        scored = [(name, m) for name, m in models_out.items() if m.get("mae") is not None]
        if scored:
            winner_name, best = min(scored, key=lambda item: item[1]["mae"])
        else:
            winner_name = trained_name
            best = models_out.get(trained_name) or next(iter(models_out.values()))
        display_name = DISPLAY_NAMES.get(winner_name, winner_name)

        return {
            "available": True,
            "model_name": display_name,
            "mae": best["mae"],
            "mape_pct": best["mape_pct"],
            "r2": best["r2"],
            "trained_rows": best["trained_rows"],
            "test_rows": best["test_rows"],
            "feature_importance": best["feature_importance"],
            "models": models_out,
        }
    except Exception as exc:
        print(f"[{__name__}] failed to compute delay metrics: {exc!r}")
        return _unavailable()
