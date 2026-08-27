"""New (real-data) module - live evaluation metrics for the production
crowd/demand prediction model.

Powers the crowd/demand section of the "AI Prediction" dashboard page.
The crowd model and the demand model are the SAME trained artifact in
this codebase (see prediction_service.forecast_demand, which just calls
predict_crowd() repeatedly for future hours) - either a
RandomForestRegressor or an XGBRegressor (whichever had the lower
held-out MAE at training time, see colab_training/train_crowd_model.py)
predicting passenger_count for a station/hour slot. Everything below is
computed from the REAL datasets/passenger_flow.csv + datasets/stations.csv
and the REAL trained crowd_model.pkl - nothing is hardcoded.

Design notes:
- Rebuilds the exact same (station_id, hour, day_of_week, is_weekend,
  is_peak_hour) -> passenger_count training table that
  colab_training/train_crowd_model.py builds via _real_dataset_builder.py
  (duplicated here rather than imported, since colab_training/ is
  intentionally standalone with no `app` package dependency), then
  re-creates its
  train_test_split(test_size=0.2, random_state=42) to get the identical
  held-out test rows.
- The model itself only predicts a passenger COUNT, not a crowd-level
  class, so "accuracy" / "Macro-F1" / confusion matrix are computed by
  converting counts into the app's own CrowdLevel buckets
  (app/enums/crowd_level.py: low/moderate/high/critical via occupancy
  ratio, thresholds 0.4/0.7/0.9). The dataset has no per-station real
  capacity figure, so an implied network-wide capacity is derived from
  the real crowding_index column already in passenger_flow.csv
  (capacity = passenger_count / crowding_index, median across rows) -
  the SAME implied capacity is used to bucket both actual and predicted
  counts, so the comparison stays apples-to-apples.
- Cached for the life of the process - the dataset/model are static
  files, so recomputing on every 30s poll would be wasted CPU.
"""
import os
from functools import lru_cache

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    mean_absolute_error,
    r2_score,
)
from sklearn.model_selection import train_test_split

MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "saved_models", "crowd_model.pkl")
DATASET_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "datasets")
STATIONS_CSV = os.path.join(DATASET_DIR, "stations.csv.gz")
PASSENGER_FLOW_CSV = os.path.join(DATASET_DIR, "passenger_flow.csv.gz")
# Gzipped to stay under GitHub's 100MB file limit - pd.read_csv infers
# the compression from the ".gz" extension, no other change needed.

FEATURES = ["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour"]
TARGET = "passenger_count"

CLASSES = ["low", "moderate", "high", "critical"]

def _bucket(ratio: float) -> str:
    if ratio < 0.4:
        return "low"
    if ratio < 0.7:
        return "moderate"
    if ratio < 0.9:
        return "high"
    return "critical"

def _norm_key(city: str, name: str) -> tuple[str, str]:
    return city.strip().lower(), name.strip().lower()

def _station_id_map() -> dict:
    """Same cleaning/ordering as colab_training/_real_dataset_builder.py
    ::_station_id_map, kept in sync manually so training and evaluation
    never drift apart."""
    stations = pd.read_csv(STATIONS_CSV).rename(
        columns={"City": "city", "Station": "station_name", "Line": "line",
                 "Latitude": "latitude", "Longitude": "longitude"}
    )
    for col in ["city", "station_name", "line"]:
        stations[col] = stations[col].astype(str).str.strip()
    stations = stations.drop_duplicates(subset=["city", "station_name"])
    stations = stations.dropna(subset=["city", "station_name", "line", "latitude", "longitude"])
    stations = stations.sort_values(["city", "line", "station_name"]).reset_index(drop=True)
    stations["station_id"] = stations.index + 1
    return dict(zip(
        stations.apply(lambda r: _norm_key(r["city"], r["station_name"]), axis=1),
        stations["station_id"],
    ))

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
        "accuracy": None,
        "macro_f1": None,
        "critical_recall": None,
        "trained_rows": None,
        "test_rows": None,
        "classes": [],
        "confusion_matrix": [],
        "feature_importance": [],
        "models": {},
    }

DISPLAY_NAMES = {"random_forest": "Random Forest", "xgboost": "XGBoost"}

def _evaluate_one(model, model_features, X_test, y_test_arr, implied_capacity, trained_rows) -> dict:
    """Same evaluation _compute_crowd_metrics used to do for a single
    model - now factored out so it can run once per candidate model
    (currently random_forest and xgboost) instead of only the winner."""
    predicted = model.predict(X_test[model_features])

    mae = float(mean_absolute_error(y_test_arr, predicted))
    r2 = float(r2_score(y_test_arr, predicted))

    nonzero = y_test_arr > 0
    mape = float(np.mean(np.abs((y_test_arr[nonzero] - predicted[nonzero]) / y_test_arr[nonzero])) * 100) if nonzero.any() else None

    actual_status = np.array([_bucket(v / implied_capacity) for v in y_test_arr])
    predicted_status = np.array([_bucket(v / implied_capacity) for v in predicted])

    accuracy = float(accuracy_score(actual_status, predicted_status))
    macro_f1 = float(f1_score(actual_status, predicted_status, labels=CLASSES, average="macro", zero_division=0))
    matrix = confusion_matrix(actual_status, predicted_status, labels=CLASSES)

    critical_idx = CLASSES.index("critical")
    critical_row = matrix[critical_idx]
    critical_total = int(critical_row.sum())
    critical_recall = float(critical_row[critical_idx] / critical_total) if critical_total > 0 else None

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
        "accuracy": round(accuracy, 4),
        "macro_f1": round(macro_f1, 4),
        "critical_recall": round(critical_recall, 4) if critical_recall is not None else None,
        "trained_rows": trained_rows,
        "test_rows": len(X_test),
        "classes": CLASSES,
        "confusion_matrix": matrix.tolist(),
        "feature_importance": feature_importance,
    }

@lru_cache(maxsize=1)
def compute_crowd_metrics() -> dict:
    bundle = _load_model()
    if bundle is None:
        return _unavailable()

    if not (os.path.exists(STATIONS_CSV) and os.path.exists(PASSENGER_FLOW_CSV)):
        print(f"[{__name__}] missing dataset files under {DATASET_DIR}")
        return _unavailable()

    try:
        model_features = bundle.get("features", FEATURES)
        # Evaluate every saved candidate (random_forest, xgboost), not
        # just the winner - falls back to the single `model` key for
        # any older .pkl trained before both were saved.
        trained_name = bundle.get("model_name", "random_forest")
        candidates = bundle.get("models") or {trained_name: bundle["model"]}

        station_id_map = _station_id_map()

        raw = pd.read_csv(PASSENGER_FLOW_CSV)
        raw["city"] = raw["city"].astype(str).str.strip()
        raw["station_name"] = raw["station_name"].astype(str).str.strip()
        raw["_key"] = raw.apply(lambda r: _norm_key(r["city"], r["station_name"]), axis=1)
        raw["station_id"] = raw["_key"].map(station_id_map)
        raw = raw.dropna(subset=["station_id"])
        raw["station_id"] = raw["station_id"].astype(int)
        raw["entries"] = raw["entries"].clip(lower=0)
        raw["exits"] = raw["exits"].clip(lower=0)
        raw["passenger_count"] = raw["entries"] + raw["exits"]
        raw["is_peak_hour"] = ((raw["hour"].between(8, 11)) | (raw["hour"].between(17, 20))).astype(int)

        idx_mask = raw["crowding_index"] > 0.05
        implied_capacity = float((raw.loc[idx_mask, "passenger_count"] / raw.loc[idx_mask, "crowding_index"]).median())

        grouped = (
            raw.groupby(["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour"])
            ["passenger_count"].mean().round().astype(int).reset_index()
        )

        X = grouped[FEATURES]
        y = grouped[TARGET]
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
        y_test_arr = y_test.to_numpy()

        models_out = {}
        for name, candidate_model in candidates.items():
            evaluated = _evaluate_one(
                candidate_model, model_features, X_test, y_test_arr,
                implied_capacity, len(X_train),
            )
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
            "accuracy": best["accuracy"],
            "macro_f1": best["macro_f1"],
            "critical_recall": best["critical_recall"],
            "trained_rows": best["trained_rows"],
            "test_rows": best["test_rows"],
            "classes": CLASSES,
            "confusion_matrix": best["confusion_matrix"],
            "feature_importance": best["feature_importance"],
            "models": models_out,
        }
    except Exception as exc:
        print(f"[{__name__}] failed to compute crowd metrics: {exc!r}")
        return _unavailable()
