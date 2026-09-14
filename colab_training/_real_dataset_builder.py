"""
2nd-generation real-data training table builder.

Matches EXACTLY the ID-assignment logic in
app/database/seed_real_data.py so that a model trained here lines up
with the station_id / train internal-id values the deployed DB will
actually have:
  - station_id (int) = 1-based row position in stations.csv.gz after
    the SAME drop_duplicates(subset=["station_id"]) + dropna(...) used
    in seed_real_data.py (there were 0 dupes/NaNs in this dataset, so
    it's a direct 1..N row-order mapping).
  - train internal id (int) = 1-based row position in trains.csv.gz
    (no filtering applied in seed_real_data.py either).

Feature/column names match app/ai_engine/prediction/delay_predictor.py
exactly: station_id, hour, day_of_week, is_weekend, is_peak_hour,
passenger_count, capacity_passengers, train_age_days.
"""
import os
from datetime import datetime

import pandas as pd

DATASET_DIR = os.path.join(os.path.dirname(__file__), "..", "datasets", "source")
STATIONS_CSV = os.path.join(DATASET_DIR, "stations.csv.gz")
TRAINS_CSV = os.path.join(DATASET_DIR, "trains.csv.gz")
PASSENGER_FLOW_CSV = os.path.join(DATASET_DIR, "passenger_flow.csv.gz")
TRAIN_OPERATIONS_CSV = os.path.join(DATASET_DIR, "train_operations.csv.gz")

TODAY = pd.Timestamp(datetime.now().date())  # single consistent "now" for the whole build


def _station_id_map() -> dict[str, int]:
    stations = pd.read_csv(STATIONS_CSV)
    for col in ["station_id", "city", "line", "station_name"]:
        stations[col] = stations[col].astype(str).str.strip()
    stations = stations.drop_duplicates(subset=["station_id"]).dropna(
        subset=["station_id", "city", "line", "station_name", "latitude", "longitude"]
    ).reset_index(drop=True)
    stations["int_station_id"] = stations.index + 1
    return dict(zip(stations["station_id"], stations["int_station_id"]))


def _train_info_map() -> pd.DataFrame:
    """Returns a DataFrame keyed by train_id (string) with the internal
    int id, capacity_passengers, and train_age_days (relative to TODAY)
    - same fields/order seed_real_data.py assigns to app.models.train.Train."""
    trains = pd.read_csv(TRAINS_CSV)
    trains["train_id"] = trains["train_id"].astype(str).str.strip()
    trains["commissioned_date"] = pd.to_datetime(trains["commissioned_date"])
    trains = trains.reset_index(drop=True)
    trains["int_train_id"] = trains.index + 1
    trains["train_age_days"] = (TODAY - trains["commissioned_date"]).dt.days.astype(float)
    return trains[["train_id", "int_train_id", "capacity_passengers", "train_age_days"]]


def _crowd_table(station_map: dict[str, int]) -> pd.DataFrame:
    df = pd.read_csv(PASSENGER_FLOW_CSV)
    df["station_id"] = df["station_id"].astype(str).str.strip().map(station_map)
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


def build_crowd_dataset() -> pd.DataFrame:
    station_map = _station_id_map()
    return _crowd_table(station_map)


def build_delay_dataset() -> pd.DataFrame:
    """Row-level (not grouped) - capacity_passengers/train_age_days are
    real per-train continuous values, so grouping early would average
    away exactly the signal these 2 features exist to capture."""
    station_map = _station_id_map()
    train_info = _train_info_map()
    crowd_table = _crowd_table(station_map)

    df = pd.read_csv(TRAIN_OPERATIONS_CSV)
    df["station_id"] = df["station_id"].astype(str).str.strip().map(station_map)
    df["train_id"] = df["train_id"].astype(str).str.strip()
    df = df.dropna(subset=["station_id"])
    df["station_id"] = df["station_id"].astype(int)

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
        print(f"delay dataset: dropped {dropped} row(s) with unknown train_id")

    df = df.merge(
        crowd_table,
        on=["station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour"],
        how="left",
    )
    # A handful of (station, hour, day_of_week, is_weekend, is_peak_hour)
    # combos in train_operations may have no matching passenger_flow rows;
    # fall back to that station's overall average rather than dropping data.
    station_avg = crowd_table.groupby("station_id")["passenger_count"].mean()
    df["passenger_count"] = df["passenger_count"].fillna(df["station_id"].map(station_avg))
    df["passenger_count"] = df["passenger_count"].fillna(crowd_table["passenger_count"].mean())

    weather_code = {"Sunny": 0, "Overcast": 1, "Rainy": 2, "Stormy": 3}
    df["weather_code"] = df["weather"].map(weather_code).fillna(0).astype(int)

    return df[[
        "station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour",
        "passenger_count", "capacity_passengers", "train_age_days", "weather_code", "delay_minutes",
    ]]


def save_all(output_dir: str) -> dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    crowd_path = os.path.join(output_dir, "real_crowd_data.csv")
    delay_path = os.path.join(output_dir, "real_delay_data.csv")

    crowd_df = build_crowd_dataset()
    crowd_df.to_csv(crowd_path, index=False)

    delay_df = build_delay_dataset()
    delay_df.to_csv(delay_path, index=False)

    return {"crowd": crowd_path, "delay": delay_path}


def build_frequency_dataset() -> pd.DataFrame:
    """Same slot-level table as crowd, with recommended_frequency_minutes
    derived from passenger_count (higher demand -> shorter headway)."""
    df = build_crowd_dataset()
    MIN_FREQUENCY, MAX_FREQUENCY = 3, 15
    max_count = df["passenger_count"].max() or 1
    normalized = df["passenger_count"] / max_count
    df["recommended_frequency_minutes"] = (
        MAX_FREQUENCY - normalized * (MAX_FREQUENCY - MIN_FREQUENCY)
    ).round(1)
    return df


if __name__ == "__main__":
    paths = save_all(os.path.join(os.path.dirname(__file__), "output"))
    for name, path in paths.items():
        df = pd.read_csv(path)
        print(f"{name}: {len(df)} rows -> {path}")
        print(df.head(3))
        print()
