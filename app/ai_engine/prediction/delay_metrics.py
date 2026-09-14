"""New (real-data) module - live evaluation metrics for the production
delay-prediction model.

Powers the delay section of the "AI Prediction" dashboard page, same
role as crowd_metrics.py plays for the crowd/demand model. Everything
below is computed from the REAL datasets/stations.csv,
datasets/passenger_flow.csv, datasets/train_operations.csv and
datasets/trains.csv, and the REAL trained delay_model.pkl - nothing is
hardcoded.

Design notes:
- Rebuilds the exact same 8-feature (station_id, hour, day_of_week,
  is_weekend, is_peak_hour, passenger_count, capacity_passengers,
  train_age_days) -> delay_minutes training table that
  colab_training/train_delay_model.py builds via
  _real_dataset_builder.py (duplicated here rather than imported,
  since colab_training/ is intentionally standalone with no `app`
  package dependency - same reasoning as crowd_metrics.py), then
  re-creates its train_test_split(test_size=0.2, random_state=42) to
  get the identical held-out test rows.
- Delay is a pure regression target (minutes of delay) - there's no
  natural class bucketing for it the way crowd has CrowdLevel, so this
  only reports MAE/MAPE/R2 and feature importance, unlike
  CrowdModelMetrics which also has accuracy/macro_f1/confusion_matrix.
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

from app.utils.timezone import business_today

MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "saved_models", "delay_model.pkl")
DATASET_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "datasets", "source")
STATIONS_CSV = os.path.join(DATASET_DIR, "stations.csv.gz")
PASSENGER_FLOW_CSV = os.path.join(DATASET_DIR, "passenger_flow.csv.gz")
TRAIN_OPERATIONS_CSV = os.path.join(DATASET_DIR, "train_operations.csv.gz")
TRAINS_CSV = os.path.join(DATASET_DIR, "trains.csv.gz")

# MEMORY FIX: both passenger_flow.csv.gz and train_operations.csv.gz
# are large production datasets. A plain pd.read_csv() on either
# pulled every column (most of them unused here) and every row into
# RAM in one shot. This module only needs the columns below, so both
# are read with usecols=..., and both are streamed in bounded
# CSV_CHUNK_SIZE-row chunks rather than parsed whole - see
# _crowd_table() and _delay_table() for what each chunk is reduced to
# before the next one is read.
PASSENGER_FLOW_USECOLS = ["station_id", "hour", "day_of_week", "is_weekend", "entries", "exits"]
TRAIN_OPERATIONS_USECOLS = [
    "station_id", "train_id", "scheduled_arrival", "delay_arrival_min", "weather",
]
CSV_CHUNK_SIZE = 100_000

# Kept in sync with delay_predictor.py / colab_training/train_delay_model.py:
# the shipped delay_model.pkl is the 8-feature real-data generation (adds
# capacity_passengers + train_age_days on top of the original 6). This used
# to be 6 features here, which silently broke this dashboard card (the
# X_test[model_features] selection below would KeyError on the 2 missing
# columns, get swallowed by the try/except in compute_delay_metrics(), and
# fall back to "no trained model" even though delay_model.pkl loads fine).
FEATURES = ["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour",
            "passenger_count", "capacity_passengers", "train_age_days", "weather_code"]
TARGET = "delay_minutes"

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

WEATHER_CODE = {"Sunny": 0, "Overcast": 1, "Rainy": 2, "Stormy": 3}

def _station_id_map() -> dict[str, int]:
    """Same cleaning/ordering as colab_training/_real_dataset_builder.py
    ::_station_id_map and app/database/seed_real_data.py (which assigns
    the real DB station.id the exact same way), kept in sync manually
    so training and evaluation never drift apart.

    Bug fix: this used to ignore the dataset's own `station_id` column
    and invent a fresh 1..N numbering by sorting stations by (city,
    line, station_name), joining passenger_flow/train_operations back
    on a (city, station_name) key. That numbering never matched the
    row-order numbering the training builder/seeder actually assign, so
    every evaluation row got a scrambled station_id relative to what
    the model was trained on - see crowd_metrics.py for the full
    writeup of the same bug there. Mapping the native station_id string
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

def _train_info_map() -> pd.DataFrame:
    """Real per-train capacity_passengers/train_age_days - mirrors
    colab_training/_real_dataset_builder.py::_train_info_map so this
    dashboard evaluates the model against the exact same real per-train
    values (never invented) that delay_predictor.py reads from the DB
    and that train_delay_model.py trained on."""
    trains = pd.read_csv(TRAINS_CSV)
    trains["train_id"] = trains["train_id"].astype(str).str.strip()
    trains["commissioned_date"] = pd.to_datetime(trains["commissioned_date"])
    trains = trains.reset_index(drop=True)
    # BUGFIX (naive datetime / timezone handling): pd.Timestamp.now()
    # reads the naive server-local clock, which can disagree with the
    # app's configured business timezone (and drifts train_age_days by
    # a day right around midnight depending on what timezone the
    # process happens to run in) - same class of bug already fixed in
    # delay_predictor.py's _real_train_age_days for this identical
    # feature. See app/utils/timezone.py.
    today = pd.Timestamp(business_today())
    trains["train_age_days"] = (today - trains["commissioned_date"]).dt.days.astype(float)
    return trains[["train_id", "capacity_passengers", "train_age_days"]]

def _crowd_table(station_id_map: dict) -> pd.DataFrame:
    """Same real passenger_count table crowd_metrics.py builds - the
    delay model was trained with passenger_count as a feature, so the
    training table needs it too.

    MEMORY FIX: streams passenger_flow.csv.gz in bounded
    CSV_CHUNK_SIZE-row chunks (only PASSENGER_FLOW_USECOLS columns)
    and reduces each chunk to a partial sum/count per (station_id,
    hour, day_of_week, is_weekend, is_peak_hour) group immediately,
    instead of materializing the whole file as one DataFrame. The
    partial sums are combined and divided once at the end (mean =
    sum/count, rounded to int) in the same sorted group-key order a
    single-shot groupby(...).mean() would produce, so the result is
    unchanged - only one CSV_CHUNK_SIZE-row chunk plus these small
    running per-group totals are ever resident in memory at once."""
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
    """Row-level (not grouped by station/hour/day only) - capacity_passengers
    and train_age_days are real per-train continuous values, so grouping
    them away before merging (like the old 6-feature version of this
    function did) would lose exactly the signal those 2 features exist to
    capture. Mirrors colab_training/_real_dataset_builder.py::build_delay_dataset.

    MEMORY FIX: every row of train_operations.csv.gz is genuinely
    needed here (this table stays row-level, it's never aggregated
    away), but only TRAIN_OPERATIONS_USECOLS columns are - so the file
    is read with usecols=... and streamed in bounded
    CSV_CHUNK_SIZE-row chunks, with each chunk immediately reduced to
    its final derived/merged columns before the next chunk is parsed,
    instead of holding the full wide (17-column) file in memory while
    deriving columns on it."""
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
        # Bug fix: the shipped delay_model.pkl was trained with a 9th
        # feature, weather_code (see colab_training/_real_dataset_builder.py
        # ::build_delay_dataset and delay_predictor.py), but this table
        # never built that column - X_test[model_features] below raised a
        # KeyError on every request, which the try/except in
        # compute_delay_metrics() swallowed into a permanent "no trained
        # model" empty state for the whole delay dashboard card.
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
    """Same shape as crowd_metrics._evaluate_one, minus the
    classification-only fields (accuracy/macro_f1/confusion_matrix)
    that don't apply to a pure regression target like delay minutes."""
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
        # A handful of (station, hour, day_of_week, is_weekend, is_peak_hour)
        # combos in train_operations may have no matching passenger_flow
        # rows; fall back to that station's overall average rather than
        # leaving passenger_count as NaN (same fallback the training-side
        # build_delay_dataset() uses, so this doesn't diverge from what the
        # model was actually trained on).
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
            "trained_rows": best["trained_rows"],
            "test_rows": best["test_rows"],
            "feature_importance": best["feature_importance"],
            "models": models_out,
        }
    except Exception as exc:
        print(f"[{__name__}] failed to compute delay metrics: {exc!r}")
        return _unavailable()