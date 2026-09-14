"""Milestone 2 - AI Prediction Module: crowd prediction model training.

Trains on the REAL 2nd-generation dataset (stations.csv.gz,
passenger_flow.csv.gz), using the exact station_id integer mapping
app/database/seed_real_data.py assigns (see _real_dataset_builder.py),
so a model trained here lines up with the station_id values seeded
into Postgres.

Trains BOTH RandomForest and XGBoost candidates and picks whichever had
the lower held-out MAE (see `train()` below) as `model`/`model_name`,
but the saved bundle also keeps BOTH fitted candidates under
`models: {"random_forest": ..., "xgboost": ...}` so the dashboard can
show both predictions side by side (see predict_crowd's docstring in
app/ai_engine/prediction/crowd_predictor.py, and
app/ai_engine/model_bundle.py's AI_MODEL_LIGHT_MODE for how that's
trimmed back to one model on memory-constrained deployments).

Standalone script - meant to be run in Google Colab (see
train_metroflow_models_colab.ipynb in this same folder), or locally
with `python colab_training/train_crowd_model.py` from the backend repo root if you
prefer. It has NO dependency on the `app` package - it never runs as
part of `uvicorn app.main:app`, so it never costs you CPU just from
running the backend.
"""
import os

import joblib
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_absolute_percentage_error
from sklearn.model_selection import train_test_split
try:
    from xgboost import XGBRegressor
    _HAS_XGB = True
except ImportError:
    _HAS_XGB = False

from _real_dataset_builder import build_crowd_dataset

FEATURES = ["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour"]
TARGET = "passenger_count"

MODEL_DIR = os.path.join(os.path.dirname(__file__), "output")
MODEL_PATH = os.path.join(MODEL_DIR, "crowd_model.pkl")

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
    df = build_crowd_dataset()
    X = df[FEATURES]
    y = df[TARGET]

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    # Bug fix (Passenger Analytics widget showing ~0 for every city except
    # Delhi/Kolkata): `passenger_count` spans wildly different scales across
    # cities (Delhi averages ~5700/hour, Pune/Bhopal average ~20-40/hour -
    # see docs/passenger-analytics-model-selection.md). Selecting the "best" model by raw MAE lets
    # a model that's good at Delhi's huge numbers win the comparison purely
    # by dominating the absolute-error sum, even if it's systematically
    # wrong (including negative, clamped to 0) for every smaller city -
    # those cities' errors are individually tiny in absolute terms, so they
    # barely move the MAE needle regardless of how wrong they are
    # proportionally. That's exactly the failure mode that made
    # `all_stations_traffic_pattern()` sum to ~0 for every city except the
    # one or two with the largest ridership.
    #
    # MAPE (mean absolute PERCENTAGE error) instead weighs every station
    # roughly equally in relative terms, so a model has to actually predict
    # each city's own scale reasonably well to win - not just nail
    # whichever city happens to have the biggest numbers.
    results = {}
    for name, build in CANDIDATES.items():
        model = build()
        model.fit(X_train, y_train)
        predictions = model.predict(X_test)
        results[name] = {
            "model": model,
            "mae": mean_absolute_error(y_test, predictions),
            "mape": mean_absolute_percentage_error(y_test, predictions),
        }

    # NOTE: sklearn's mean_absolute_percentage_error divides by
    # max(eps, |y_true|) - with many (station, slot) combos genuinely
    # having 0 average passengers (late-night hours at low-traffic
    # stations), a handful of those rows turn a small absolute error
    # into a multi-trillion-percent MAPE that swamps the metric and
    # picks the objectively worse candidate (higher MAE, lower R2) in
    # practice. Select by MAE instead, matching how
    # train_delay_model.py/train_frequency_model.py already choose
    # their winner; MAPE is still computed/returned for visibility.
    best_name = min(results, key=lambda n: results[n]["mae"])
    best_model = results[best_name]["model"]
    best_mae = results[best_name]["mae"]

    os.makedirs(MODEL_DIR, exist_ok=True)
    # app/ai_engine/model_bundle.py + crowd_predictor.py already read an
    # optional bundle["models"] dict (name -> fitted model) to show both
    # candidates side by side on the dashboard, falling back to just the
    # winner if it's absent. Populate it here so that feature actually
    # has data - AI_MODEL_LIGHT_MODE (see model_bundle.py) trims this
    # back down to a single model at load time on memory-constrained
    # deployments, so shipping both here costs nothing there.
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
        "candidate_mape": {name: r["mape"] for name, r in results.items()},
    }

if __name__ == "__main__":
    metrics = train()
    comparison = ", ".join(
        f"{name}=MAE:{mae:.2f}/MAPE:{metrics['candidate_mape'][name]:.1%}"
        for name, mae in metrics["candidate_mae"].items()
    )
    print(f"Crowd model trained. Selected={metrics['model_name']} (MAE={metrics['mae']:.2f} passengers) "
          f"[{comparison}], saved to {metrics['model_path']}")
