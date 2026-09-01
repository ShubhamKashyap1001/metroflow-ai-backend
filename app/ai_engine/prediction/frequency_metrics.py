"""New (real-data) module - live evaluation metrics for the production
train-frequency recommendation model.

Powers the frequency section of the "AI Prediction" dashboard page,
same role as crowd_metrics.py plays for the crowd/demand model.
Everything below is computed from the REAL datasets/stations.csv and
datasets/passenger_flow.csv and the REAL trained frequency_model.pkl -
nothing is hardcoded.

Design notes:
- Rebuilds the exact same (station_id, hour, day_of_week, is_weekend,
  is_peak_hour) -> recommended_frequency_minutes training table that
  colab_training/train_frequency_model.py builds (real passenger_count
  table from _real_dataset_builder.py, then the same
  demand-to-headway derivation as train_frequency_model.py::
  _derive_target), then re-creates its
  train_test_split(test_size=0.2, random_state=42) to get the
  identical held-out test rows.
- Frequency recommendation is a pure regression target (minutes
  between trains) - there's no natural class bucketing for it the way
  crowd has CrowdLevel, so this only reports MAE/MAPE/R2 and feature
  importance, unlike CrowdModelMetrics which also has
  accuracy/macro_f1/confusion_matrix.
- Cached for the life of the process - the dataset/model are static
  files, so recomputing on every poll would be wasted CPU.
"""
import os
from functools import lru_cache

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import train_test_split

MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "saved_models", "frequency_model.pkl")
DATASET_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "datasets")
STATIONS_CSV = os.path.join(DATASET_DIR, "stations.csv.gz")
PASSENGER_FLOW_CSV = os.path.join(DATASET_DIR, "passenger_flow.csv.gz")

FEATURES = ["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour"]
TARGET = "recommended_frequency_minutes"

MIN_FREQUENCY = 3
MAX_FREQUENCY = 15

DISPLAY_NAMES = {"random_forest": "Random Forest", "xgboost": "XGBoost"}

def _station_id_map() -> dict[str, int]:
    """Same cleaning/ordering as colab_training/_real_dataset_builder.py
    ::_station_id_map and app/database/seed_real_data.py (which assigns
    the real DB station.id the exact same way), kept in sync manually
    so training and evaluation never drift apart.

    Bug fix: this used to ignore the dataset's own `station_id` column
    and invent a fresh 1..N numbering by sorting stations by (city,
    line, station_name), joining passenger_flow back on a (city,
    station_name) key. That numbering never matched the row-order
    numbering the training builder/seeder actually assign, so every
    evaluation row got a scrambled station_id relative to what the
    model was trained on - see crowd_metrics.py for the full writeup
    of the same bug there. Mapping the native station_id string
    directly fixes it here too.
    """
    stations = pd.read_csv(STATIONS_CSV)
    for col in ["station_id", "city", "line", "station_name"]:
        stations[col] = stations[col].astype(str).str.strip()
    stations = stations.drop_duplicates(subset=["station_id"]).dropna(
        subset=["station_id", "city", "line", "station_name", "latitude", "longitude"]
    ).reset_index(drop=True)
    stations["int_station_id"] = stations.index + 1
    return dict(zip(stations["station_id"], stations["int_station_id"]))

def _crowd_table(station_id_map: dict) -> pd.DataFrame:
    df = pd.read_csv(PASSENGER_FLOW_CSV)
    df["station_id"] = df["station_id"].astype(str).str.strip().map(station_id_map)
    df = df.dropna(subset=["station_id"])
    df["station_id"] = df["station_id"].astype(int)

    df["entries"] = df["entries"].clip(lower=0)
    df["exits"] = df["exits"].clip(lower=0)
    df["passenger_count"] = df["entries"] + df["exits"]
    df["is_peak_hour"] = ((df["hour"].between(8, 11)) | (df["hour"].between(17, 20))).astype(int)

    grouped = (
        df.groupby(["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour"])
        ["passenger_count"].mean().round().astype(int).reset_index()
    )
    return grouped

def _derive_target(df: pd.DataFrame) -> pd.DataFrame:
    """Identical to train_frequency_model.py::_derive_target - higher
    demand maps to a shorter (smaller-minute) recommended headway."""
    max_count = df["passenger_count"].max() or 1
    normalized = df["passenger_count"] / max_count
    df[TARGET] = MAX_FREQUENCY - normalized * (MAX_FREQUENCY - MIN_FREQUENCY)
    df[TARGET] = df[TARGET].round(1)
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
def compute_frequency_metrics() -> dict:
    bundle = _load_model()
    if bundle is None:
        return _unavailable()

    if not (os.path.exists(STATIONS_CSV) and os.path.exists(PASSENGER_FLOW_CSV)):
        print(f"[{__name__}] missing dataset files under {DATASET_DIR}")
        return _unavailable()

    try:
        model_features = bundle.get("features", FEATURES)
        trained_name = bundle.get("model_name", "random_forest")
        candidates = bundle.get("models") or {trained_name: bundle["model"]}

        station_id_map = _station_id_map()
        table = _crowd_table(station_id_map)
        table = _derive_target(table)

        X = table[FEATURES]
        y = table[TARGET]
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
        y_test_arr = y_test.to_numpy()

        models_out = {}
        for name, candidate_model in candidates.items():
            evaluated = _evaluate_one(candidate_model, model_features, X_test, y_test_arr, len(X_train))
            evaluated["model_name"] = DISPLAY_NAMES.get(name, name)
            models_out[name] = evaluated

        best = models_out.get(trained_name) or next(iter(models_out.values()))
        display_name = DISPLAY_NAMES.get(trained_name, trained_name)

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
        print(f"[{__name__}] failed to compute frequency metrics: {exc!r}")
        return _unavailable()
