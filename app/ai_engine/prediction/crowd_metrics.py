import json
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

METRICS_JSON_PATH = os.path.join(os.path.dirname(__file__), "..", "saved_models", "crowd_model_metrics.json")
DATASET_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "datasets", "source")
STATIONS_CSV = os.path.join(DATASET_DIR, "stations.csv.gz")
PASSENGER_FLOW_CSV = os.path.join(DATASET_DIR, "passenger_flow.csv.gz")

PASSENGER_FLOW_USECOLS = [
    "station_id", "hour", "day_of_week", "is_weekend", "entries", "exits", "crowding_index",
]
CSV_CHUNK_SIZE = 100_000

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

def _station_id_map() -> dict[str, int]:
    stations = pd.read_csv(STATIONS_CSV)
    for col in ["station_id", "city", "line", "station_name"]:
        stations[col] = stations[col].astype(str).str.strip()
    stations = stations.drop_duplicates(subset=["station_id"]).dropna(
        subset=["station_id", "city", "line", "station_name", "latitude", "longitude"]
    ).reset_index(drop=True)
    stations["int_station_id"] = stations.index + 1
    return dict(zip(stations["station_id"], stations["int_station_id"]))

def _passenger_flow_aggregates(station_id_map: dict) -> tuple[pd.DataFrame, float]:
    partial_group_sums: list[pd.DataFrame] = []
    capacity_ratio_chunks: list[np.ndarray] = []

    for chunk in pd.read_csv(PASSENGER_FLOW_CSV, usecols=PASSENGER_FLOW_USECOLS, chunksize=CSV_CHUNK_SIZE):
        chunk = chunk.copy()
        chunk["station_id"] = chunk["station_id"].astype(str).str.strip().map(station_id_map)
        chunk = chunk.dropna(subset=["station_id"])
        chunk["station_id"] = chunk["station_id"].astype(int)
        chunk["entries"] = chunk["entries"].clip(lower=0)
        chunk["exits"] = chunk["exits"].clip(lower=0)
        chunk["passenger_count"] = chunk["entries"] + chunk["exits"]
        chunk["is_peak_hour"] = ((chunk["hour"].between(8, 11)) | (chunk["hour"].between(17, 20))).astype(int)

        idx_mask = chunk["crowding_index"] > 0.05
        if idx_mask.any():
            capacity_ratio_chunks.append(
                (chunk.loc[idx_mask, "passenger_count"] / chunk.loc[idx_mask, "crowding_index"]).to_numpy()
            )

        chunk_grouped = (
            chunk.groupby(FEATURES)["passenger_count"].agg(["sum", "count"]).reset_index()
        )
        partial_group_sums.append(chunk_grouped)

    if partial_group_sums:
        combined = pd.concat(partial_group_sums, ignore_index=True)
        combined = combined.groupby(FEATURES)[["sum", "count"]].sum().reset_index()
        combined["passenger_count"] = (combined["sum"] / combined["count"]).round().astype(int)
        grouped = combined[FEATURES + ["passenger_count"]].sort_values(FEATURES).reset_index(drop=True)
    else:
        grouped = pd.DataFrame(columns=FEATURES + ["passenger_count"])

    if capacity_ratio_chunks:
        implied_capacity = float(np.median(np.concatenate(capacity_ratio_chunks)))
    else:
        implied_capacity = float("nan")

    return grouped, implied_capacity

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

DISPLAY_NAMES = {
    "random_forest": "Random Forest",
    "xgboost": "XGBoost",

    "random_forest_tuned": "Random Forest",
    "xgboost_tuned": "XGBoost",
}

def _evaluate_one(model, model_features, X_test, y_test_arr, implied_capacity, trained_rows) -> dict:
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

def _live_compute_crowd_metrics() -> dict:
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

        grouped, implied_capacity = _passenger_flow_aggregates(station_id_map)

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
