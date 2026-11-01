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

from _real_dataset_builder import build_frequency_dataset

FEATURES = ["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour"]
TARGET = "recommended_frequency_minutes"

MIN_FREQUENCY = 3
MAX_FREQUENCY = 15

MODEL_DIR = os.path.join(os.path.dirname(__file__), "output")
MODEL_PATH = os.path.join(MODEL_DIR, "frequency_model.pkl")

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
    df = build_frequency_dataset()
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

    joblib.dump({
        "model": best_model,
        "features": FEATURES,
        "model_name": best_name,
        "min_frequency": MIN_FREQUENCY,
        "max_frequency": MAX_FREQUENCY,
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
    print(f"Frequency model trained. Selected={metrics['model_name']} (MAE={metrics['mae']:.3f} min) "
          f"[{comparison}], saved to {metrics['model_path']}")
