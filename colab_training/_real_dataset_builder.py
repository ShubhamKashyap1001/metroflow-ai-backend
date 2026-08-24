"""Builds a real-data training table (replacement for the old synthetic
`ridership_data.csv`) out of `datasets/stations.csv`, `datasets/passenger_flow.csv`
and `datasets/train_operations.csv`.

Output columns match exactly what train_crowd_model.py / train_delay_model.py /
train_frequency_model.py already expect, so no other script needs to change:

    station_id, hour, day_of_week, is_weekend, is_peak_hour,
    passenger_count, delay_minutes

`station_id` uses the EXACT same deterministic ordering as
`app/database/seed_real_data.py` (sort stations by city -> line -> name,
then 1..N), so a model trained on this file lines up with the station_id
values seeded into Postgres.

Standalone - no `app` package import, works the same in Colab or locally.
"""
import os

import pandas as pd

DATASET_DIR = os.path.join(os.path.dirname(__file__), "..", "datasets")
STATIONS_CSV = os.path.join(DATASET_DIR, "stations.csv")
PASSENGER_FLOW_CSV = os.path.join(DATASET_DIR, "passenger_flow.csv")
TRAIN_OPERATIONS_CSV = os.path.join(DATASET_DIR, "train_operations.csv")

def _norm_key(city: str, name: str) -> tuple[str, str]:
    return city.strip().lower(), name.strip().lower()

def _station_id_map() -> dict[tuple[str, str], int]:
    """Same cleaning/ordering as app/database/seed_real_data.py::_clean_stations,
    kept in sync manually so the two never drift apart."""
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

def _crowd_table(station_id_map: dict) -> pd.DataFrame:
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

def _delay_table(station_id_map: dict) -> pd.DataFrame:
    df = pd.read_csv(TRAIN_OPERATIONS_CSV)
    df["city"] = df["city"].astype(str).str.strip()
    df["station_name"] = df["station_name"].astype(str).str.strip()
    df["_key"] = df.apply(lambda r: _norm_key(r["city"], r["station_name"]), axis=1)
    df["station_id"] = df["_key"].map(station_id_map)
    df = df.dropna(subset=["station_id"])
    df["station_id"] = df["station_id"].astype(int)

    df["scheduled_arrival"] = pd.to_datetime(df["scheduled_arrival"])
    df["hour"] = df["scheduled_arrival"].dt.hour
    df["day_of_week"] = df["scheduled_arrival"].dt.weekday
    df["delay_arrival_min"] = df["delay_arrival_min"].fillna(0).clip(lower=0)

    grouped = (
        df.groupby(["station_id", "hour", "day_of_week"])
        ["delay_arrival_min"].mean().round(2).reset_index()
        .rename(columns={"delay_arrival_min": "delay_minutes"})
    )
    return grouped

def build_real_dataset() -> pd.DataFrame:
    station_id_map = _station_id_map()
    crowd = _crowd_table(station_id_map)
    delay = _delay_table(station_id_map)

    merged = crowd.merge(delay, on=["station_id", "hour", "day_of_week"], how="left")
    merged["delay_minutes"] = merged["delay_minutes"].fillna(0.0)

    return merged[[
        "station_id", "hour", "day_of_week", "is_weekend", "is_peak_hour",
        "passenger_count", "delay_minutes",
    ]]

def save_dataset(path: str) -> str:
    df = build_real_dataset()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False)
    return path

if __name__ == "__main__":
    out = save_dataset(os.path.join(os.path.dirname(__file__), "output", "real_ridership_data.csv"))
    df = pd.read_csv(out)
    print(f"Built real training table: {len(df)} (station, hour, day_of_week) rows -> {out}")
    print(df.head())
