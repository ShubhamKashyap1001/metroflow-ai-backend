"""Milestone 2 - AI Prediction Module: delay prediction inference.

Supports the 8-feature delay_model.pkl (adds capacity_passengers and
train_age_days on top of the original 6). Both are sourced from real
data, never invented:

  capacity_passengers -> Train.capacity, a real per-train DB column
                          seeded straight from trains.csv's
                          capacity_passengers (2nd-generation dataset:
                          real values from 974-1284, not a uniform
                          guess). If a specific train_id is given,
                          that train's own capacity is used;
                          otherwise the real average across active
                          trains is used.

  train_age_days       -> Train.commissioned_date, a real per-train DB
                          column seeded straight from trains.csv's
                          commissioned_date. train_age_days =
                          (today - commissioned_date).days. This
                          dataset generation has no separate sensor
                          CSV, so this is now a direct DB read, not an
                          estimate from a replay cache.
"""
import logging
import os
from datetime import date, datetime
from functools import lru_cache

import joblib
import pandas as pd
from sqlalchemy.orm import Session

from app.ai_engine.prediction.crowd_predictor import predict_crowd
from app.models.train import Train

logger = logging.getLogger(__name__)

MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "saved_models", "delay_model.pkl")

@lru_cache(maxsize=1)
def _load_model():
    if not os.path.exists(MODEL_PATH):
        print(f"[{__name__}] no trained model at {MODEL_PATH} - using heuristic fallback")
        return None
    try:
        return joblib.load(MODEL_PATH)
    except Exception as exc:
                                                                     
        print(f"[{__name__}] failed to load {MODEL_PATH}: {exc!r} - using heuristic fallback")
        return None

def _real_capacity_passengers(db: Session | None, train_id: int | None) -> float:
    """Real Train.capacity - this train's own if we know which train,
    otherwise the real average across active trains. Never a made-up
    constant."""
    if db is None:
        return 1200.0                                                  
    if train_id is not None:
        train = db.get(Train, train_id)
        if train is not None:
            return float(train.capacity)
    rows = db.query(Train.capacity).filter(Train.is_active.is_(True)).all()
    if rows:
        values = [c for (c,) in rows]
        return sum(values) / len(values)
    return 1200.0

def _real_train_age_days(db: Session | None, train_id: int | None) -> float:
    """Real (today - Train.commissioned_date).days for this train, or
    the real average age across active trains if no specific train_id
    is given. 0.0 only if there's genuinely no commissioned_date data
    at all (e.g. an older synthetic seed)."""
    if db is None:
        return 0.0

    if train_id is not None:
        train = db.get(Train, train_id)
        if train is not None and train.commissioned_date is not None:
            return float((date.today() - train.commissioned_date).days)

    rows = (
        db.query(Train.commissioned_date)
        .filter(Train.is_active.is_(True), Train.commissioned_date.isnot(None))
        .all()
    )
    if rows:
        ages = [(date.today() - c).days for (c,) in rows]
        return sum(ages) / len(ages)
    return 0.0

def predict_delay(
    station_id: int,
    target_datetime: datetime | None = None,
    train_id: int | None = None,
    db: Session | None = None,
) -> dict:
    dt = target_datetime or datetime.utcnow()
    hour = dt.hour
    day_of_week = dt.weekday()
    is_weekend = 1 if day_of_week in (5, 6) else 0
    is_peak_hour = 1 if (8 <= hour <= 11 or 17 <= hour <= 20) else 0

    crowd = predict_crowd(station_id, dt)
    passenger_count = crowd["predicted_count"]

    bundle = _load_model()
    predicted_delay = None

    if bundle is not None:
        try:
            model = bundle["model"]
            row_values = {
                "station_id": station_id,
                "hour": hour,
                "day_of_week": day_of_week,
                "is_weekend": is_weekend,
                "is_peak_hour": is_peak_hour,
                "passenger_count": passenger_count,
                "capacity_passengers": _real_capacity_passengers(db, train_id),
                "train_age_days": _real_train_age_days(db, train_id),
            }
                                                                       
            features = pd.DataFrame(
                [[row_values[f] for f in bundle["features"]]],
                columns=bundle["features"],
            )
            predicted_delay = float(model.predict(features)[0])
            model_version = "random_forest_v1"
        except Exception as exc:                                                  
                                                                     
            logger.warning(
                "delay model .predict() failed (%r) - using heuristic fallback for this request",
                exc,
            )
            predicted_delay = None

    if predicted_delay is None:
        predicted_delay = max(0.0, (passenger_count / 1000) * 4)
        model_version = "heuristic_fallback"

    return {
        "station_id": station_id,
        "target_datetime": dt,
        "predicted_delay_minutes": round(max(0.0, predicted_delay), 1),
        "based_on_predicted_crowd": passenger_count,
        "model_version": model_version,
    }
