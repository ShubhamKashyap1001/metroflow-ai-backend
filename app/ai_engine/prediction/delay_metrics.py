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

MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "saved_models", "delay_model.pkl")
DATASET_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "datasets")
STATIONS_CSV = os.path.join(DATASET_DIR, "stations.csv.gz")
PASSENGER_FLOW_CSV = os.path.join(DATASET_DIR, "passenger_flow.csv.gz")
TRAIN_OPERATIONS_CSV = os.path.join(DATASET_DIR, "train_operations.csv.gz")
TRAINS_CSV = os.path.join(DATASET_DIR, "trains.csv.gz")

# Kept in sync with delay_predictor.py / colab_training/train_delay_model.py:
# the shipped delay_model.pkl is the 8-feature real-data generation (adds
# capacity_passengers + train_age_days on top of the original 6). This used
# to be 6 features here, which silently broke this dashboard card (the
# X_test[model_features] selection below would KeyError on the 2 missing
# columns, get swallowed by the try/except in compute_delay_metrics(), and
# fall back to "no trained model" even though delay_model.pkl loads fine).
FEATURES = ["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour",
            "passenger_count", "capacity_passengers", "train_age_days"]
TARGET = "delay_minutes"

DISPLAY_NAMES = {"random_forest": "Random Forest", "xgboost": "XGBoost"}

def _norm_key(city: str, name: str) -> tuple[str, str]:
    return city.strip().lower(), name.strip().lower()

def _station_id_map() -> dict:
    """Same cleaning/ordering as colab_training/_real_dataset_builder.py
    ::_station_id_map and crowd_metrics.py::_station_id_map, kept in
    sync manually so training and evaluation never drift apart."""
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
    today = pd.Timestamp(pd.Timestamp.now().date())
    trains["train_age_days"] = (today - trains["commissioned_date"]).dt.days.astype(float)
    return trains[["train_id", "capacity_passengers", "train_age_days"]]

def _crowd_table(station_id_map: dict) -> pd.DataFrame:
    """Same real passenger_count table crowd_metrics.py builds - the
    delay model was trained with passenger_count as a feature, so the
    training table needs it too."""
    df = pd.read_csv(PASSENGER_FLOW_CSV)
    df["city"] = df["city"].astype(str).str.strip()
    df["station_name"] = df["station_name"].astype(str).str.strip()
    df["_key"] = df.apply(lambda r: _norm_key(r["city"], r["station_name"]), axis=1)
    df["station_id"] = df["_key"].map(station_id_map)
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

def _delay_table(station_id_map: dict, train_info: pd.DataFrame) -> pd.DataFrame:
    """Row-level (not grouped by station/hour/day only) - capacity_passengers
    and train_age_days are real per-train continuous values, so grouping
    them away before merging (like the old 6-feature version of this
    function did) would lose exactly the signal those 2 features exist to
    capture. Mirrors colab_training/_real_dataset_builder.py::build_delay_dataset."""
    df = pd.read_csv(TRAIN_OPERATIONS_CSV)
    df["city"] = df["city"].astype(str).str.strip()
    df["station_name"] = df["station_name"].astype(str).str.strip()
    df["_key"] = df.apply(lambda r: _norm_key(r["city"], r["station_name"]), axis=1)
    df["station_id"] = df["_key"].map(station_id_map)
    df = df.dropna(subset=["station_id"])
    df["station_id"] = df["station_id"].astype(int)

    df["train_id"] = df["train_id"].astype(str).str.strip()

    df["scheduled_arrival"] = pd.to_datetime(df["scheduled_arrival"])
    df["hour"] = df["scheduled_arrival"].dt.hour
    df["day_of_week"] = df["scheduled_arrival"].dt.weekday
    df["is_weekend"] = (df["day_of_week"] >= 5).astype(int)
    df["is_peak_hour"] = ((df["hour"].between(8, 11)) | (df["hour"].between(17, 20))).astype(int)
    df["delay_minutes"] = df["delay_arrival_min"].fillna(0).clip(lower=0)

    df = df.merge(train_info, on="train_id", how="left")
    before = len(df)
    df = df.dropna(subset=["capacity_passengers", "train_age_days"])
    dropped = before - len(df)
    if dropped:
        print(f"[{__name__}] delay metrics: dropped {dropped} row(s) with unknown train_id")

    return df[["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour",
               "delay_minutes", "capacity_passengers", "train_age_days"]]

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
        print(f"[{__name__}] failed to compute delay metrics: {exc!r}")
        return _unavailable()