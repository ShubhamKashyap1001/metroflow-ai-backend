"""Milestone 2 - AI Prediction Module: delay prediction model training.

Trains on the REAL 2nd-generation dataset (train_operations.csv.gz
joined with trains.csv.gz for real per-train capacity_passengers and
train_age_days), using the exact station_id/train integer mapping
app/database/seed_real_data.py assigns (see _real_dataset_builder.py).

8 features - the extra 2 (capacity_passengers, train_age_days) are
real per-train values, never invented, matching what
app/ai_engine/prediction/delay_predictor.py reads from the DB
(Train.capacity, Train.commissioned_date) at inference time. Feature
names match delay_predictor.py exactly, so no naming-mismatch shim is
needed on the inference side.

Trains BOTH RandomForest and XGBoost candidates and picks whichever had
the lower held-out MAE (see `train()` below) - but the saved
production bundle stores ONLY that winning estimator (`model` +
`model_name`), not the losing candidate, to keep the shipped .pkl and
its in-memory footprint down to a single trained model.

Standalone script - meant to be run in Google Colab (see
train_metroflow_models_colab.ipynb in this same folder), or locally
with `python colab_training/train_delay_model.py` from the backend repo root if you
prefer. It has NO dependency on the `app` package - it never runs as
part of `uvicorn app.main:app`, so it never costs you CPU just from
running the backend.
"""
import os

import joblib
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import train_test_split
try:
    from xgboost import XGBRegressor
    _HAS_XGB = True
except ImportError:
    _HAS_XGB = False

from _real_dataset_builder import build_delay_dataset

FEATURES = ["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour",
            "passenger_count", "capacity_passengers", "train_age_days", "weather_code"]
TARGET = "delay_minutes"

MODEL_DIR = os.path.join(os.path.dirname(__file__), "output")
MODEL_PATH = os.path.join(MODEL_DIR, "delay_model.pkl")

CANDIDATES = {
    "random_forest": lambda: RandomForestRegressor(n_estimators=60, max_depth=8, random_state=42),
}
if _HAS_XGB:
    CANDIDATES["xgboost"] = lambda: XGBRegressor(
        n_estimators=200, max_depth=6, learning_rate=0.05,
        subsample=0.9, colsample_bytree=0.9, random_state=42,
        objective="reg:squarederror",
    )

def train() -> dict:
    df = build_delay_dataset()
    X = df[FEATURES]
    y = df[TARGET]

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    results = {}
    for name, build in CANDIDATES.items():
        model = build()
        model.fit(X_train, y_train)
        predictions = model.predict(X_test)
        results[name] = {"model": model, "mae": mean_absolute_error(y_test, predictions)}

    best_name = min(results, key=lambda n: results[n]["mae"])
    best_model = results[best_name]["model"]
    best_mae = results[best_name]["mae"]

    os.makedirs(MODEL_DIR, exist_ok=True)
    # Bundle keeps both fitted candidates under 'models' - see
    # train_crowd_model.py's train() for why.
    joblib.dump({
        "model": best_model,
        "model_name": best_name,
        "features": FEATURES,
        "models": {name: r["model"] for name, r in results.items()},
    }, MODEL_PATH)

    return {
        "mae": best_mae,
        "model_path": MODEL_PATH,
        "samples": len(df),
        "model_name": best_name,
        "candidate_mae": {name: r["mae"] for name, r in results.items()},
    }

if __name__ == "__main__":
    metrics = train()
    comparison = ", ".join(f"{name}={mae:.3f}" for name, mae in metrics["candidate_mae"].items())
    print(f"Delay model trained. Selected={metrics['model_name']} (MAE={metrics['mae']:.3f} minutes) "
          f"[{comparison}], saved to {metrics['model_path']}")
