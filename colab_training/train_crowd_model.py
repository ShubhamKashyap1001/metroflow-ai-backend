"""Milestone 2 - AI Prediction Module: crowd prediction model training.

Trains a RandomForestRegressor to predict passenger_count for a
station at a given hour/day, using the REAL passenger_flow.csv /
stations.csv dataset (built into a training table by
_real_dataset_builder.py).

Standalone script - meant to be run in Google Colab (see
train_metroflow_models_colab.ipynb in this same folder), or locally
with `python colab_training/train_crowd_model.py` from the backend repo root if you
prefer. It has NO dependency on the `app` package - it never runs as
part of `uvicorn app.main:app`, so it never costs you CPU just from
running the backend.
"""
import os

import joblib
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import train_test_split

from _real_dataset_builder import save_dataset

FEATURES = ["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour"]
TARGET = "passenger_count"

MODEL_DIR = os.path.join(os.path.dirname(__file__), "output")
MODEL_PATH = os.path.join(MODEL_DIR, "crowd_model.pkl")
DATASET_PATH = os.path.join(os.path.dirname(__file__), "output", "real_ridership_data.csv")

def load_dataset() -> pd.DataFrame:
    if not os.path.exists(DATASET_PATH):
        os.makedirs(os.path.dirname(DATASET_PATH), exist_ok=True)
        save_dataset(DATASET_PATH)
    return pd.read_csv(DATASET_PATH)

def train() -> dict:
    df = load_dataset()
    X = df[FEATURES]
    y = df[TARGET]

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    model = RandomForestRegressor(n_estimators=60, max_depth=8, random_state=42)
    model.fit(X_train, y_train)

    predictions = model.predict(X_test)
    mae = mean_absolute_error(y_test, predictions)

    os.makedirs(MODEL_DIR, exist_ok=True)
    joblib.dump({"model": model, "features": FEATURES}, MODEL_PATH)

    return {"mae": mae, "model_path": MODEL_PATH, "samples": len(df)}

if __name__ == "__main__":
    metrics = train()
    print(f"Crowd model trained. MAE={metrics['mae']:.2f} passengers, saved to {metrics['model_path']}")
