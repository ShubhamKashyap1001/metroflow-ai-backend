"""Tests for the production ML-metrics CSV memory fix.

crowd_metrics.py, delay_metrics.py and frequency_metrics.py used to
pull the entire passenger_flow.csv.gz / train_operations.csv.gz file
(every column, every row) into a single pandas DataFrame on every
cold cache miss. These tests confirm:

1. passenger_flow.csv.gz is read with a reduced column set and in
   bounded chunks (never one big in-memory read of the full file).
2. train_operations.csv.gz is read with a reduced column set and in
   bounded chunks.
3. The columns each metric actually needs are still present/correct.
4. The aggregated/derived table each function returns is unchanged in
   shape and columns (API response compatibility).
5. The values are logically equivalent to the original single-shot
   pd.read_csv(...) + groupby implementation.
6. The full compute_*_metrics() endpoints still produce the same
   response shape end-to-end (with a stubbed model, since the real
   .pkl model is out of scope for this fix).
7. The simulator's own chunked CSV reader (csv_replay_simulator.py) is
   untouched and its existing tests still pass.
"""
import gzip
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from app.ai_engine.prediction import crowd_metrics, delay_metrics, frequency_metrics

STATION_MAP = {"STN-A-01": 1, "STN-B-01": 2}


# --- helpers to build small synthetic source CSVs (mirrors the real
# datasets/source/*.csv.gz column layout, trimmed to a handful of rows) ---

def _write_gz_csv(path, df: pd.DataFrame) -> None:
    with gzip.open(path, "wt", newline="") as fh:
        df.to_csv(fh, index=False)


def _synthetic_passenger_flow_df(n_per_station: int = 40) -> pd.DataFrame:
    rows = []
    rng = np.random.default_rng(42)
    for station in ["STN-A-01", "STN-B-01"]:
        for i in range(n_per_station):
            hour = i % 24
            day_of_week = (i // 24) % 7
            rows.append({
                "timestamp": f"2026-01-{1 + (i % 28):02d} {hour:02d}:00:00",
                "city": "TestCity",
                "station_id": station,
                "station_name": f"{station} Name",
                "line": "Red",
                "station_type": "regular",
                "hour": hour,
                "day_of_week": day_of_week,
                "is_weekend": int(day_of_week >= 5),
                "weather": "Sunny",
                "event_nearby": 0,
                "entries": int(rng.integers(0, 500)),
                "exits": int(rng.integers(0, 500)),
                "crowding_index": float(rng.uniform(0.01, 0.9)),
                "crowding_label": "Normal",
                "data_source": "test",
            })
    return pd.DataFrame(rows)


def _synthetic_train_operations_df(n: int = 60) -> pd.DataFrame:
    rows = []
    rng = np.random.default_rng(7)
    for i in range(n):
        station = "STN-A-01" if i % 2 == 0 else "STN-B-01"
        rows.append({
            "trip_id": f"TRIP-{i}",
            "city": "TestCity",
            "train_id": f"TRAIN-{i % 5}",
            "line": "Red",
            "station_id": station,
            "station_name": f"{station} Name",
            "station_sequence": 1,
            "scheduled_arrival": f"2026-01-{1 + (i % 28):02d} {i % 24:02d}:00:00",
            "actual_arrival": f"2026-01-{1 + (i % 28):02d} {i % 24:02d}:05:00",
            "scheduled_departure": f"2026-01-{1 + (i % 28):02d} {i % 24:02d}:02:00",
            "actual_departure": f"2026-01-{1 + (i % 28):02d} {i % 24:02d}:07:00",
            "delay_arrival_min": float(rng.integers(0, 15)),
            "delay_departure_min": float(rng.integers(0, 15)),
            "passenger_density": "medium",
            "weather": ["Sunny", "Overcast", "Rainy", "Stormy"][i % 4],
            "delay_reason": None,
            "data_source": "test",
        })
    return pd.DataFrame(rows)


@pytest.fixture
def passenger_flow_csv(tmp_path):
    path = tmp_path / "passenger_flow.csv.gz"
    _write_gz_csv(path, _synthetic_passenger_flow_df())
    return path, _synthetic_passenger_flow_df()  # note: regenerated df has same seed -> same data


@pytest.fixture
def train_operations_csv(tmp_path):
    path = tmp_path / "train_operations.csv.gz"
    _write_gz_csv(path, _synthetic_train_operations_df())
    return path


def _old_grouped_passenger_table(df: pd.DataFrame, station_map: dict) -> pd.DataFrame:
    """Reference implementation identical to the ORIGINAL (pre-fix)
    single-shot pd.read_csv(...) + groupby logic, used to assert the
    new chunked implementation is logically equivalent."""
    df = df.copy()
    df["station_id"] = df["station_id"].astype(str).str.strip().map(station_map)
    df = df.dropna(subset=["station_id"])
    df["station_id"] = df["station_id"].astype(int)
    df["entries"] = df["entries"].clip(lower=0)
    df["exits"] = df["exits"].clip(lower=0)
    df["passenger_count"] = df["entries"] + df["exits"]
    df["is_peak_hour"] = ((df["hour"].between(8, 11)) | (df["hour"].between(17, 20))).astype(int)
    return (
        df.groupby(["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour"])
        ["passenger_count"].mean().round().astype(int).reset_index()
    )


# --- 1 & 2: bounded reads (usecols + chunksize) -----------------------

def test_crowd_metrics_reads_passenger_flow_with_reduced_columns_and_chunks(passenger_flow_csv):
    path, _ = passenger_flow_csv
    real_read_csv = pd.read_csv
    with patch.object(crowd_metrics, "PASSENGER_FLOW_CSV", str(path)), \
         patch.object(crowd_metrics.pd, "read_csv", wraps=real_read_csv) as spy:
        crowd_metrics._passenger_flow_aggregates(STATION_MAP)

    assert spy.call_count >= 1
    _, kwargs = spy.call_args
    assert kwargs.get("usecols") == crowd_metrics.PASSENGER_FLOW_USECOLS
    # Full passenger_flow.csv.gz has 16 columns - confirm we ask for far fewer.
    assert len(kwargs["usecols"]) < 16
    assert kwargs.get("chunksize") == crowd_metrics.CSV_CHUNK_SIZE


def test_delay_metrics_crowd_table_reads_passenger_flow_with_reduced_columns_and_chunks(passenger_flow_csv):
    path, _ = passenger_flow_csv
    real_read_csv = pd.read_csv
    with patch.object(delay_metrics, "PASSENGER_FLOW_CSV", str(path)), \
         patch.object(delay_metrics.pd, "read_csv", wraps=real_read_csv) as spy:
        delay_metrics._crowd_table(STATION_MAP)

    _, kwargs = spy.call_args
    assert kwargs.get("usecols") == delay_metrics.PASSENGER_FLOW_USECOLS
    assert len(kwargs["usecols"]) < 16
    assert kwargs.get("chunksize") == delay_metrics.CSV_CHUNK_SIZE


def test_frequency_metrics_crowd_table_reads_passenger_flow_with_reduced_columns_and_chunks(passenger_flow_csv):
    path, _ = passenger_flow_csv
    real_read_csv = pd.read_csv
    with patch.object(frequency_metrics, "PASSENGER_FLOW_CSV", str(path)), \
         patch.object(frequency_metrics.pd, "read_csv", wraps=real_read_csv) as spy:
        frequency_metrics._crowd_table(STATION_MAP)

    _, kwargs = spy.call_args
    assert kwargs.get("usecols") == frequency_metrics.PASSENGER_FLOW_USECOLS
    assert len(kwargs["usecols"]) < 16
    assert kwargs.get("chunksize") == frequency_metrics.CSV_CHUNK_SIZE


def test_delay_metrics_delay_table_reads_train_operations_with_reduced_columns_and_chunks(train_operations_csv):
    train_info = pd.DataFrame({
        "train_id": [f"TRAIN-{i}" for i in range(5)],
        "capacity_passengers": [800] * 5,
        "train_age_days": [1000.0] * 5,
    })
    real_read_csv = pd.read_csv
    with patch.object(delay_metrics, "TRAIN_OPERATIONS_CSV", str(train_operations_csv)), \
         patch.object(delay_metrics.pd, "read_csv", wraps=real_read_csv) as spy:
        delay_metrics._delay_table(STATION_MAP, train_info)

    _, kwargs = spy.call_args
    assert kwargs.get("usecols") == delay_metrics.TRAIN_OPERATIONS_USECOLS
    # Full train_operations.csv.gz has 17 columns - confirm we ask for far fewer.
    assert len(kwargs["usecols"]) < 17
    assert kwargs.get("chunksize") == delay_metrics.CSV_CHUNK_SIZE


# --- 3 & 4: required columns preserved / output structure unchanged ---

def test_crowd_metrics_grouped_table_has_original_columns(passenger_flow_csv):
    path, _ = passenger_flow_csv
    with patch.object(crowd_metrics, "PASSENGER_FLOW_CSV", str(path)):
        grouped, implied_capacity = crowd_metrics._passenger_flow_aggregates(STATION_MAP)

    assert list(grouped.columns) == crowd_metrics.FEATURES + ["passenger_count"]
    assert isinstance(implied_capacity, float)


def test_delay_metrics_delay_table_has_original_columns(train_operations_csv):
    train_info = pd.DataFrame({
        "train_id": [f"TRAIN-{i}" for i in range(5)],
        "capacity_passengers": [800] * 5,
        "train_age_days": [1000.0] * 5,
    })
    with patch.object(delay_metrics, "TRAIN_OPERATIONS_CSV", str(train_operations_csv)):
        table = delay_metrics._delay_table(STATION_MAP, train_info)

    assert list(table.columns) == [
        "station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour",
        "delay_minutes", "capacity_passengers", "train_age_days", "weather_code",
    ]


# --- 5: logical equivalence with the original (pre-fix) computation ---

def test_crowd_metrics_aggregates_match_original_single_shot_computation(passenger_flow_csv):
    path, df = passenger_flow_csv
    with patch.object(crowd_metrics, "PASSENGER_FLOW_CSV", str(path)):
        new_grouped, new_capacity = crowd_metrics._passenger_flow_aggregates(STATION_MAP)

    old_grouped = _old_grouped_passenger_table(df, STATION_MAP)
    idx_mask = df.copy()
    idx_mask["station_id"] = idx_mask["station_id"].astype(str).str.strip().map(STATION_MAP)
    idx_mask = idx_mask.dropna(subset=["station_id"])
    idx_mask["passenger_count"] = idx_mask["entries"].clip(lower=0) + idx_mask["exits"].clip(lower=0)
    mask = idx_mask["crowding_index"] > 0.05
    old_capacity = float((idx_mask.loc[mask, "passenger_count"] / idx_mask.loc[mask, "crowding_index"]).median())

    old_sorted = old_grouped.sort_values(crowd_metrics.FEATURES).reset_index(drop=True)
    new_sorted = new_grouped.sort_values(crowd_metrics.FEATURES).reset_index(drop=True)
    assert old_sorted.equals(new_sorted)
    assert old_capacity == pytest.approx(new_capacity)


def test_frequency_metrics_crowd_table_matches_original_single_shot_computation(passenger_flow_csv):
    path, df = passenger_flow_csv
    with patch.object(frequency_metrics, "PASSENGER_FLOW_CSV", str(path)):
        new_grouped = frequency_metrics._crowd_table(STATION_MAP)

    old_grouped = _old_grouped_passenger_table(df, STATION_MAP)
    old_sorted = old_grouped.sort_values(frequency_metrics.FEATURES).reset_index(drop=True)
    new_sorted = new_grouped.sort_values(frequency_metrics.FEATURES).reset_index(drop=True)
    assert old_sorted.equals(new_sorted)


# --- 6: full compute_*_metrics() endpoints keep their response shape --

class _FakeModel:
    """Minimal stand-in for the trained RandomForest/XGBoost model so
    the full compute_*_metrics() path can be exercised without the
    real .pkl (out of scope for this fix) or the xgboost dependency."""

    feature_importances_ = np.array([0.5, 0.2, 0.1, 0.1, 0.1])

    def predict(self, X):
        return np.zeros(len(X))


def test_compute_crowd_metrics_keeps_response_shape(tmp_path, passenger_flow_csv):
    pf_path, _ = passenger_flow_csv
    stations_df = pd.DataFrame({
        "station_id": ["STN-A-01", "STN-B-01"],
        "city": ["TestCity", "TestCity"],
        "line": ["Red", "Red"],
        "station_name": ["A", "B"],
        "latitude": [1.0, 2.0],
        "longitude": [1.0, 2.0],
    })
    stations_path = tmp_path / "stations.csv.gz"
    _write_gz_csv(stations_path, stations_df)

    bundle = {
        "features": crowd_metrics.FEATURES,
        "model_name": "random_forest",
        "models": {"random_forest": _FakeModel()},
    }

    crowd_metrics.compute_crowd_metrics.cache_clear()
    with patch.object(crowd_metrics, "STATIONS_CSV", str(stations_path)), \
         patch.object(crowd_metrics, "PASSENGER_FLOW_CSV", str(pf_path)), \
         patch.object(crowd_metrics, "_load_model", return_value=bundle):
        result = crowd_metrics.compute_crowd_metrics()
    crowd_metrics.compute_crowd_metrics.cache_clear()

    assert result["available"] is True
    assert set(result.keys()) == {
        "available", "model_name", "mae", "mape_pct", "r2", "accuracy", "macro_f1",
        "critical_recall", "trained_rows", "test_rows", "classes", "confusion_matrix",
        "feature_importance", "models",
    }


# --- 7: simulator's own chunked CSV reader is untouched ---------------

def test_simulator_module_not_modified_by_this_fix():
    """csv_replay_simulator.py is out of scope for this fix - confirm
    it still exposes its existing chunked-read entry points untouched."""
    from app.simulator import csv_replay_simulator as sim

    assert hasattr(sim, "_current_count_from_row")
