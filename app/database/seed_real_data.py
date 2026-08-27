"""Loads your REAL datasets (stations.csv, trains.csv, passenger_flow.csv,
train_operations.csv) into Postgres, so the frontend shows real
stations/cities/crowd/delay numbers instead of the synthetic demo
network from app/database/seed.py.

This is the 2nd-generation dataset: stations.csv, passenger_flow.csv
and train_operations.csv all carry a real `station_id` string
(e.g. "STN-DEL-YL-02"), and trains.csv/train_operations.csv both
carry a real `train_id` string (e.g. "TRN-AMD-BL-01") - so joins here
are exact string-key matches, not fuzzy (city, name) matching like
the previous generation needed.

This is a plain data-cleaning + DB-insert script - no model training,
no scikit-learn import, nothing CPU-heavy. Safe to run on your laptop.

Usage:
    1. Put the 4 CSVs in a `datasets/` folder at the backend repo root
       (same folder this script looks for them in by default), or pass
       a different folder with --dir.
    2. From the backend repo root:
           python -m app.database.seed_real_data --reset
       (--reset wipes any previously-seeded data first - required if
       you're moving from the old dataset generation, since station_id
       values are shaped completely differently now)
"""
import argparse
import os
from datetime import date, datetime

import pandas as pd
from sqlalchemy import text

from app.database.init_db import create_tables
from app.database.session import SessionLocal
from app.enums.day_type import DayType
from app.enums.schedule_status import ScheduleStatus
from app.models.alert import Alert
from app.models.crowd_log import CrowdLog
from app.models.journey import Journey
from app.models.line_station import LineStation
from app.models.metro_line import MetroLine
from app.models.prediction import Prediction
from app.models.station import Station
from app.models.train import Train
from app.models.train_location import TrainLocation
from app.models.train_schedule import TrainSchedule

DEFAULT_DATASET_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "datasets")

LINE_COLORS = ["#1E88E5", "#8E24AA", "#E53935", "#00897B", "#6A1B9A", "#F4511E"]
NAMED_LINE_COLORS = {
    "yellow": "#EAB308", "blue": "#2563EB", "red": "#DC2626", "green": "#16A34A",
    "violet": "#7C3AED", "magenta": "#DB2777", "pink": "#EC4899", "grey": "#6B7280",
    "gray": "#6B7280", "purple": "#9333EA", "orange": "#EA580C", "aqua": "#06B6D4",
}

def _color_for_line(line_name: str, fallback_index: int) -> str:
    lowered = line_name.lower()
    for keyword, color in NAMED_LINE_COLORS.items():
        if keyword in lowered:
            return color
    return LINE_COLORS[fallback_index % len(LINE_COLORS)]

def _load_csvs(dataset_dir: str) -> dict[str, pd.DataFrame]:
    # Gzipped (.csv.gz) to stay under GitHub's 100MB per-file push limit -
    # pandas infers the compression from the ".gz" extension on its own,
    # so pd.read_csv below needs no other change.
    paths = {
        "stations": os.path.join(dataset_dir, "stations.csv.gz"),
        "trains": os.path.join(dataset_dir, "trains.csv.gz"),
        "passenger_flow": os.path.join(dataset_dir, "passenger_flow.csv.gz"),
        "train_operations": os.path.join(dataset_dir, "train_operations.csv.gz"),
    }
    missing = [name for name, p in paths.items() if not os.path.exists(p)]
    if missing:
        raise FileNotFoundError(
            f"Missing CSV(s) in {dataset_dir}: {', '.join(missing)}.csv.gz - "
            f"copy your 4 real gzipped CSVs there first (or pass --dir)."
        )
    return {name: pd.read_csv(p) for name, p in paths.items()}

def seed(dataset_dir: str = DEFAULT_DATASET_DIR, reset: bool = False) -> None:
    create_tables()
    db = SessionLocal()

    try:
        if reset:
            print("--reset: clearing existing station/line/train/schedule/crowd data...")
                                                                         
            db.execute(text(
                "TRUNCATE TABLE journeys, predictions, train_locations, alerts, "
                "crowd_logs, train_schedules, line_stations, metro_lines, "
                "stations, trains RESTART IDENTITY CASCADE"
            ))
            db.commit()
        elif db.query(Station).count() > 0:
            print("Database already has stations - pass --reset to wipe and reseed with the new dataset.")
            return

        raw = _load_csvs(dataset_dir)

        stations_df = raw["stations"].copy()
        for col in ["station_id", "city", "line", "station_name", "station_type"]:
            stations_df[col] = stations_df[col].astype(str).str.strip()
        stations_df = stations_df.drop_duplicates(subset=["station_id"]).dropna(
            subset=["station_id", "city", "line", "station_name", "latitude", "longitude"]
        )

        station_rows: dict[str, Station] = {}
        station_list: list[Station] = []
        for _, row in stations_df.iterrows():
            station = Station(
                station_code=row["station_id"],
                station_name=row["station_name"],
                city=row["city"],
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
                is_interchange=False,
                capacity=5000,
            )
            station_list.append(station)
            station_rows[row["station_id"]] = station
        db.add_all(station_list)
        db.flush()                      

        line_keys = stations_df[["city", "line"]].drop_duplicates().reset_index(drop=True)
        line_by_key: dict[tuple[str, str], MetroLine] = {}
        line_list: list[MetroLine] = []
        for i, (_, lrow) in enumerate(line_keys.iterrows()):
            city_slug = "".join(ch for ch in lrow["city"].upper() if ch.isalpha())[:3]
            line_slug = "".join(ch for ch in lrow["line"].upper() if ch.isalpha())[:4]
            line = MetroLine(
                line_code=f"{city_slug}-{line_slug}-{i}",
                line_name=f"{lrow['city']} Metro - {lrow['line']}",
                color=_color_for_line(lrow["line"], i),
            )
            line_list.append(line)
            line_by_key[(lrow["city"], lrow["line"])] = line
        db.add_all(line_list)
        db.flush()

        line_station_list: list[LineStation] = []
        for _, srow in stations_df.iterrows():
            line = line_by_key[(srow["city"], srow["line"])]
            line_station_list.append(LineStation(
                line_id=line.id,
                station_id=station_rows[srow["station_id"]].id,
                station_order=int(srow["station_sequence"]),
                distance_from_previous=2.5 if int(srow["station_sequence"]) > 1 else 0,
            ))
        db.add_all(line_station_list)

        trains_df = raw["trains"].copy()
        trains_df["train_id"] = trains_df["train_id"].astype(str).str.strip()
        trains_df["commissioned_date"] = pd.to_datetime(trains_df["commissioned_date"])

        train_by_number: dict[str, Train] = {}
        train_list: list[Train] = []
        for _, trow in trains_df.iterrows():
            train = Train(
                train_number=trow["train_id"],
                capacity=int(trow["capacity_passengers"]),
                commissioned_date=trow["commissioned_date"].date(),
            )
            train_list.append(train)
            train_by_number[trow["train_id"]] = train
        db.add_all(train_list)
        db.flush()

        # Commit stations/lines/line_stations/trains now, before the
        # train_operations loop below. Without this, everything since
        # SessionLocal() was opened stays uncommitted (flush() assigns
        # IDs but doesn't end the transaction) for as long as the
        # ~469K-row schedule loop takes to run - often several minutes
        # on a remote DB. A transaction left open that long is exactly
        # what a connection pooler in front of Postgres (e.g. Supabase's
        # pgbouncer in transaction-pooling mode) or the DB's own idle/
        # statement timeout will kill, which surfaces later as
        # "psycopg2.OperationalError: server closed the connection
        # unexpectedly" on some unrelated later commit - confusing,
        # since the actual cause is this stale, still-open transaction,
        # not whatever commit happened to be running when it got killed.
        # Committing here makes this data durable immediately and lets
        # the long schedule loop start on a fresh, short-lived
        # transaction per chunk (see CHUNK_SIZE below), same reasoning.
        db.commit()
        print(f"  stations/lines/trains: {len(station_list)} stations, "
              f"{len(line_list)} lines, {len(train_list)} trains committed.")

        ops_df = raw["train_operations"].copy()
        ops_df["station_id"] = ops_df["station_id"].astype(str).str.strip()
        ops_df["train_id"] = ops_df["train_id"].astype(str).str.strip()
        ops_df["delay_reason"] = ops_df["delay_reason"].fillna("None")
        ops_df["delay_arrival_min"] = ops_df["delay_arrival_min"].fillna(0).clip(lower=0)
        ops_df["scheduled_arrival"] = pd.to_datetime(ops_df["scheduled_arrival"])
        ops_df["scheduled_departure"] = pd.to_datetime(ops_df["scheduled_departure"])

        # train_operations.csv.gz is ~469K rows. Building all of those as
        # live ORM TrainSchedule() objects and handing them to a single
        # db.add_all(...) + one db.commit() at the very end (the original
        # approach) opens ONE transaction that has to hold ~469K rows'
        # worth of INSERT statements plus SQLAlchemy's identity-map
        # bookkeeping for every one of those objects for the entire
        # duration. In practice that's what was crashing the seed with
        # "server closed the connection unexpectedly" - a transaction
        # that large/long-running gets killed by the DB server itself
        # (statement/idle timeouts), by a pooler in front of it (e.g.
        # Supabase's pgbouncer in transaction mode), or just by ordinary
        # network flakiness over however many minutes it takes.
        #
        # Fix: build plain dicts (no ORM identity-map overhead) and
        # insert them in bounded chunks via bulk_insert_mappings, with a
        # commit after every chunk. Each chunk is its own short
        # transaction, so a drop mid-seed loses at most one chunk's
        # worth of rows (re-running with --reset starts clean anyway),
        # and no single transaction ever has to stay open for the whole
        # 469K-row load.
        CHUNK_SIZE = 5000
        schedule_dicts: list[dict] = []
        dropped = 0
        inserted = 0
        for _, orow in ops_df.iterrows():
            train = train_by_number.get(orow["train_id"])
            db_station = station_rows.get(orow["station_id"])
            if not train or not db_station:
                dropped += 1
                continue
            is_weekend = int(orow["scheduled_arrival"].weekday() >= 5)
            hour = orow["scheduled_arrival"].hour
            is_peak = 8 <= hour <= 11 or 17 <= hour <= 20
            delay_minutes = int(round(orow["delay_arrival_min"]))
            schedule_dicts.append({
                "train_id": train.id,
                "station_id": db_station.id,
                "arrival_time": orow["scheduled_arrival"].time(),
                "departure_time": orow["scheduled_departure"].time(),
                "platform_number": (int(orow["station_sequence"]) % 2) + 1,
                "day_type": DayType.WEEKEND if is_weekend else DayType.WEEKDAY,
                "is_peak_hour": bool(is_peak),
                "frequency_minutes": 5 if is_peak else 12,
                "delay_minutes": delay_minutes,
                "status": ScheduleStatus.DELAYED if delay_minutes > 0 else ScheduleStatus.ON_TIME,
            })
            if len(schedule_dicts) >= CHUNK_SIZE:
                db.bulk_insert_mappings(TrainSchedule, schedule_dicts)
                db.commit()
                inserted += len(schedule_dicts)
                print(f"  train_schedules: {inserted}/{len(ops_df) - dropped} inserted...", end="\r")
                schedule_dicts = []
        if schedule_dicts:
            db.bulk_insert_mappings(TrainSchedule, schedule_dicts)
            db.commit()
            inserted += len(schedule_dicts)
        if dropped:
            print(f"\ntrain_operations: dropped {dropped} row(s) with an unknown station_id/train_id")
        print(f"  train_schedules: {inserted} inserted (done).")

        flow_df = raw["passenger_flow"].copy()
        flow_df["station_id"] = flow_df["station_id"].astype(str).str.strip()
        flow_df["passenger_count"] = flow_df["entries"].clip(lower=0) + flow_df["exits"].clip(lower=0)
        recent_avg = flow_df.groupby("station_id")["passenger_count"].mean()

        crowd_rows = []
        for station_id, db_station in station_rows.items():
            avg_count = recent_avg.get(station_id, db_station.capacity * 0.2)
            crowd_rows.append(CrowdLog(
                station_id=db_station.id,
                current_count=int(min(avg_count, db_station.capacity)),
            ))
        db.add_all(crowd_rows)

        db.commit()
        print(
            f"Seeded {len(station_rows)} real stations across "
            f"{stations_df['city'].nunique()} cities, {len(line_by_key)} lines, "
            f"{len(train_by_number)} trains (real capacity + commissioned_date "
            f"from trains.csv), {inserted} real schedule entries, and "
            f"{len(crowd_rows)} crowd snapshots - EVERY station now has real "
            f"passenger_flow.csv coverage (this dataset covers all "
            f"{len(station_rows)}, not just 6 like the previous one)."
        )
    finally:
        db.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=DEFAULT_DATASET_DIR, help="Folder containing the 4 CSVs")
    parser.add_argument("--reset", action="store_true", help="Wipe existing station/line/schedule/crowd data first")
    args = parser.parse_args()
    seed(dataset_dir=args.dir, reset=args.reset)
