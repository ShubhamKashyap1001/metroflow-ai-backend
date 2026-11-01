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

    best_name = min(results, key=lambda n: results[n]["mae"])
    best_model = results[best_name]["model"]
    best_mae = results[best_name]["mae"]

    os.makedirs(MODEL_DIR, exist_ok=True)

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
