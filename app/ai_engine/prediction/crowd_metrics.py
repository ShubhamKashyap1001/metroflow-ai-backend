"""Dashboard metrics for the production crowd/demand prediction model.

Powers the crowd/demand section of the "AI Prediction" dashboard page.
The crowd model and the demand model are the SAME trained artifact in
this codebase (see prediction_service.forecast_demand, which just calls
predict_crowd() repeatedly for future hours) - either a
RandomForestRegressor or an XGBRegressor (whichever had the lower
held-out MAE at training time, see colab_training/train_crowd_model.py)
predicting passenger_count for a station/hour slot.

IMPORTANT - what actually runs in the deployed API process:
`compute_crowd_metrics()` (the function prediction_service.py calls)
only reads the small precomputed `crowd_model_metrics.json` file below
- it never touches crowd_model.pkl and never imports the real
passenger_flow/stations CSVs at request time. This is deliberate: this
file used to unpickle the model bundle AND stream the full dataset on
every cold cache miss, and - critically - to show a Random Forest vs
XGBoost comparison it had to hold BOTH fitted candidates in memory
simultaneously (on top of whatever the prediction-serving path already
has loaded via app/ai_engine/model_bundle.py). On a Render Free 512MB
instance that is a real OOM risk, even if only transient/once-per-
process. Precomputing removes that risk entirely: both candidates are
only ever loaded together OFFLINE, by
colab_training/precompute_dashboard_metrics.py, run as part of the
training/build step - never inside the running web service. The
deployed crowd_model.pkl itself only ever contains the single winning
model (see app/ai_engine/model_bundle.py), so even a bug that somehow
called the old code path could not load two models into this process.

The `_live_compute_crowd_metrics()` function below still contains the
real evaluation logic (rebuilds the exact real-data test split, runs
both candidates against it, buckets predictions into CrowdLevel
classes, computes accuracy/Macro-F1/confusion matrix/critical recall).
It is kept here as the single source of truth for that logic, but it
is only ever invoked by the offline precompute script - re-run it
(python colab_training/precompute_dashboard_metrics.py) whenever the
models are retrained, then redeploy the refreshed
crowd_model_metrics.json alongside the new crowd_model.pkl.

Design notes (for _live_compute_crowd_metrics):
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
"""
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
# Precomputed offline (see colab_training/precompute_dashboard_metrics.py)
# from a build-time bundle that has BOTH fitted candidates. This is the
# only thing the running API reads - see module docstring above.
METRICS_JSON_PATH = os.path.join(os.path.dirname(__file__), "..", "saved_models", "crowd_model_metrics.json")
DATASET_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "datasets", "source")
STATIONS_CSV = os.path.join(DATASET_DIR, "stations.csv.gz")
PASSENGER_FLOW_CSV = os.path.join(DATASET_DIR, "passenger_flow.csv.gz")
# Gzipped to stay under GitHub's 100MB file limit - pd.read_csv infers
# the compression from the ".gz" extension, no other change needed.

# MEMORY FIX: passenger_flow.csv.gz is a large production dataset (16
# columns, ~800k rows). This module only ever needs the 7 columns
# below, and only ever needs them reduced to a small per-(station,
# hour, day_of_week, is_weekend, is_peak_hour) aggregate - so a plain
# pd.read_csv(PASSENGER_FLOW_CSV) (all columns, every row materialized
# at once) was pulling the entire wide file into RAM on every cold
# cache miss. Read only these columns, and read them in bounded
# chunks (see CSV_CHUNK_SIZE) that get reduced to small partial
# aggregates immediately, so only one chunk's worth of raw rows is
# ever resident at a time.
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
    """Same cleaning/ordering as colab_training/_real_dataset_builder.py
    ::_station_id_map (and app/database/seed_real_data.py, which assigns
    the real DB station.id the exact same way) - kept in sync manually
    so training and evaluation never drift apart.

    Bug fix: this used to ignore the dataset's own `station_id` column
    (e.g. "STN-DEL-YL-01") entirely and invent a fresh 1..N numbering by
    sorting stations by (city, line, station_name), then join
    passenger_flow.csv back on a (city, station_name) key. That
    numbering never matched the row-order numbering
    _real_dataset_builder.py/seed_real_data.py actually assign (no
    sort, drop_duplicates on the native station_id string, index+1) -
    so every evaluation row got a scrambled station_id relative to what
    the model was trained on, which is what made a genuinely reasonable
    model (~470 MAE / ~48% MAPE at training time) score as R2=-0.63 /
    MAPE=1243% here. Mapping the native station_id string directly -
    exactly like the two files above - fixes that drift.
    """
    stations = pd.read_csv(STATIONS_CSV)
    for col in ["station_id", "city", "line", "station_name"]:
        stations[col] = stations[col].astype(str).str.strip()
    stations = stations.drop_duplicates(subset=["station_id"]).dropna(
        subset=["station_id", "city", "line", "station_name", "latitude", "longitude"]
    ).reset_index(drop=True)
    stations["int_station_id"] = stations.index + 1
    return dict(zip(stations["station_id"], stations["int_station_id"]))

def _passenger_flow_aggregates(station_id_map: dict) -> tuple[pd.DataFrame, float]:
    """Streams passenger_flow.csv.gz in bounded chunks (CSV_CHUNK_SIZE
    rows at a time, only PASSENGER_FLOW_USECOLS columns) and reduces
    each chunk immediately to the two things compute_crowd_metrics()
    actually needs:

    1. `grouped` - the mean passenger_count per (station_id, hour,
       day_of_week, is_weekend, is_peak_hour), i.e. exactly what
       raw.groupby(FEATURES)["passenger_count"].mean()...reset_index()
       produced before. Built incrementally via a per-chunk sum/count,
       combined across chunks, then divided once at the end - same
       arithmetic (mean = sum/count, rounded to int) and same sorted
       group-key order as the original single-shot groupby, so the
       resulting table (and therefore the train_test_split(random_state=42)
       held-out rows built from it) is unchanged.
    2. `implied_capacity` - the median of passenger_count/crowding_index
       over rows with crowding_index > 0.05. Computing an exact median
       still requires seeing every qualifying value, but only that one
       float column is accumulated across chunks (not the whole
       DataFrame), which is a small fraction of the file's memory
       footprint.

    Only ever holds one CSV_CHUNK_SIZE-row chunk plus these small
    running aggregates in memory - never the full dataset.
    """
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
    # Current production artifacts (Sept 2026 retrain) suffix the
    # winning candidate name with "_tuned" (see colab_training/
    # train_metroflow_models_colab.ipynb) - map those too so the
    # dashboard still shows a friendly label instead of the raw
    # internal model_name string.
    "random_forest_tuned": "Random Forest",
    "xgboost_tuned": "XGBoost",
}

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
    """What the running API actually calls (see
    prediction_service.get_crowd_model_metrics). Reads the small
    precomputed JSON file only - never unpickles crowd_model.pkl, never
    reads the source CSVs, never holds more than the single production
    model in this process's memory. See module docstring for why."""
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
    """Real evaluation logic - loads crowd_model.pkl and the source
    CSVs, evaluates every candidate in bundle["models"] against a fresh
    held-out split. NOT called by the running API (see module
    docstring) - only by colab_training/precompute_dashboard_metrics.py,
    offline, against a build-time bundle that has both candidates."""
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

        # Bug fix: `trained_name` is whatever colab_training picked as the
        # winner using ITS OWN dataset build/test split at training time.
        # models_out above is a fresh, independent re-evaluation (this
        # module's own dataset build + train_test_split(random_state=42)) -
        # normally identical, but the two can disagree (different data
        # snapshot since training, a chunked-vs-single-shot aggregation
        # rounding difference, etc). Trusting the stale `trained_name` in
        # that case shows an "Active" badge on a candidate whose own MAE/R2
        # displayed right next to it is visibly worse than the other card -
        # exactly the mismatch this fixes. Pick the winner from the live
        # numbers actually being displayed instead, so the badge always
        # matches what's on screen; still fall back to `trained_name` (then
        # the first candidate) if MAE is missing for every candidate.
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