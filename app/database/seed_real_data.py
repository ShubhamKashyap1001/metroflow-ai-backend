import argparse
import gzip
import os
from datetime import date, datetime

import pandas as pd
from sqlalchemy import text

from app.database.init_db import create_tables
from app.database.session import SessionLocal
from app.enums.crowd_level import CrowdLevel
from app.enums.day_type import DayType
from app.enums.schedule_status import ScheduleStatus
from app.models.alert import Alert
from app.models.crowd_log import CrowdLog
from app.models.journey import Journey
from app.models.line_station import LineStation
from app.models.metro_line import MetroLine
from app.models.prediction import Prediction
from app.models.station import Station
from app.models.station_crowd_state import StationCrowdState
from app.models.train import Train
from app.models.train_location import TrainLocation
from app.models.train_schedule import TrainSchedule
from app.models.train_schedule_history import TrainScheduleHistory

DEFAULT_DATASET_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "datasets", "source")

DEFAULT_CAPACITY = 2400

# Render Free (512MB) OOM fix: passenger_flow.csv.gz and
# train_operations.csv.gz are both several-hundred-thousand-row files.
# Loading either one fully into a DataFrame (let alone both at once,
# which is what _load_csvs() used to do for all 4 CSVs) can exceed a
# 512MB instance's memory. CSV_CHUNK_SIZE bounds how many rows of
# either file are ever resident in memory at once - everything that
# reads these two files below streams them with pd.read_csv(...,
# chunksize=CSV_CHUNK_SIZE) instead of reading them whole.
CSV_CHUNK_SIZE = 50_000

def _derive_station_capacities(flow_df: pd.DataFrame) -> dict[str, int]:
    """Reverse-engineer each station's real capacity from the dataset's
    own crowding_index column instead of hardcoding one number for
    every station (Phase 3 fix, Bug 2 - see docs/crowd-data-correctness.md).

    crowding_index is defined by the dataset as
    (entries + exits) / capacity, clipped at 1.5 for real overcrowding
    (verified: max observed crowding_index across the whole dataset is
    exactly 1.5). Solving for capacity per row
    (`(entries + exits) / crowding_index`) and taking the PER-STATION
    MEDIAN (not mean, so a handful of clipped/extreme rows can't skew
    it) recovers a stable, station-specific capacity.
    """
    df = flow_df[flow_df["crowding_index"] > 0].copy()
    df["implied_capacity"] = (df["entries"] + df["exits"]) / df["crowding_index"]
    return df.groupby("station_id")["implied_capacity"].median().round().astype(int).to_dict()


def _build_seed_crowd_rows(
    flow_df: pd.DataFrame, station_rows: dict[str, Station]
) -> tuple[list[CrowdLog], list[dict]]:
    """Seed BOTH the historical table (crowd_logs) and the live table
    (station_crowd_state) with real, correctly-computed values instead
    of the old single fabricated "average throughput" row per station
    (Phase 3 fix, Bug 3 - see docs/crowd-data-correctness.md).

    Walks each station's own real first calendar day of
    passenger_flow.csv rows (chronological order) through the SAME
    net-flow occupancy accumulator csv_replay_simulator.py uses at
    runtime (occupancy += entries - exits, clamped at 0) - so the seed
    data and the live simulator agree on what "current_count" means,
    instead of the seed using throughput and the simulator using
    occupancy. The last hour of that walk becomes each station's
    initial `station_crowd_state` row, which fixes the second half of
    Bug 3: previously station_crowd_state was left completely empty
    until the simulator's first tick, so the dashboard/heatmap had no
    live data for however long that took.
    """
    df = flow_df.sort_values(["station_id", "timestamp"])
    crowd_logs: list[CrowdLog] = []
    live_state_rows: list[dict] = []

    for station_id, group in df.groupby("station_id"):
        db_station = station_rows.get(station_id)
        if db_station is None:
            continue
        first_day = group["timestamp"].dt.date.iloc[0]
        day_rows = group[group["timestamp"].dt.date == first_day]

        capacity = db_station.capacity or DEFAULT_CAPACITY

        occupancy = 0.0
        for _, row in day_rows.iterrows():
            occupancy = max(0.0, occupancy + row["entries"] - row["exits"])

            level = CrowdLevel.from_ratio(occupancy / capacity if capacity else 0)
            crowd_logs.append(CrowdLog(
                station_id=db_station.id,
                current_count=int(round(occupancy)),
                crowd_level=level,
            ))

        live_state_rows.append({
            "station_id": db_station.id,
            "current_count": int(round(occupancy)),
            "crowd_level": level,
        })

    return crowd_logs, live_state_rows


def _derive_station_capacities_chunked(
    path: str, chunksize: int = CSV_CHUNK_SIZE
) -> dict[str, int]:
    """Memory-bounded equivalent of _derive_station_capacities() for the
    OOM fix: streams passenger_flow.csv.gz in row-bounded chunks
    (reading only the station_id/entries/exits/crowding_index columns
    it needs, not the other ~12 columns in the file) instead of
    requiring the whole file as one DataFrame, then computes the exact
    same per-station median implied-capacity over the full accumulated
    set at the end. Produces byte-for-byte the same dict as calling
    _derive_station_capacities() on the whole file at once (verified
    against this dataset while building this fix).
    """
    by_station: dict[str, list[float]] = {}
    usecols = ["station_id", "entries", "exits", "crowding_index"]
    for chunk in pd.read_csv(path, usecols=usecols, chunksize=chunksize):
        chunk = chunk.copy()
        chunk["station_id"] = chunk["station_id"].astype(str).str.strip()
        chunk = chunk[chunk["crowding_index"] > 0]
        if chunk.empty:
            continue
        implied_capacity = (chunk["entries"] + chunk["exits"]) / chunk["crowding_index"]
        for station_id, value in zip(chunk["station_id"], implied_capacity):
            by_station.setdefault(station_id, []).append(float(value))
    return {
        station_id: int(round(pd.Series(values).median()))
        for station_id, values in by_station.items()
    }


def _first_day_per_station(path: str, chunksize: int = CSV_CHUNK_SIZE) -> dict[str, date]:
    """Pass 1/2 of the chunked crowd-row build (OOM fix): finds each
    station's earliest calendar date in passenger_flow.csv.gz by
    streaming only the station_id/timestamp columns in bounded
    chunks - never the whole file, and only a single running-minimum
    date per station is kept in memory."""
    first_day: dict[str, date] = {}
    usecols = ["station_id", "timestamp"]
    for chunk in pd.read_csv(path, usecols=usecols, chunksize=chunksize):
        chunk = chunk.copy()
        chunk["station_id"] = chunk["station_id"].astype(str).str.strip()
        chunk["timestamp"] = pd.to_datetime(chunk["timestamp"])
        chunk_min = chunk.groupby("station_id")["timestamp"].min()
        for station_id, ts in chunk_min.items():
            day = ts.date()
            if station_id not in first_day or day < first_day[station_id]:
                first_day[station_id] = day
    return first_day


def _collect_first_day_flow_rows(
    path: str, first_day: dict[str, date], chunksize: int = CSV_CHUNK_SIZE
) -> pd.DataFrame:
    """Pass 2/2 of the chunked crowd-row build (OOM fix): re-streams
    passenger_flow.csv.gz and keeps only each station's own first
    calendar day of rows (the small subset _build_seed_crowd_rows()
    actually uses - typically ~1 day's worth of readings per station),
    with entries/exits clamped to 0 exactly like the non-chunked
    flow_df preprocessing in seed() used to do. The small resulting
    DataFrame is then handed to the UNMODIFIED _build_seed_crowd_rows(),
    which produces identical output to running it on the full file,
    because that function already restricts itself to each station's
    first day internally (verified against this dataset while building
    this fix)."""
    usecols = ["station_id", "timestamp", "entries", "exits"]
    matched_chunks: list[pd.DataFrame] = []
    for chunk in pd.read_csv(path, usecols=usecols, chunksize=chunksize):
        chunk = chunk.copy()
        chunk["station_id"] = chunk["station_id"].astype(str).str.strip()
        chunk["timestamp"] = pd.to_datetime(chunk["timestamp"])
        expected_day = chunk["station_id"].map(first_day)
        matched = chunk[chunk["timestamp"].dt.date == expected_day]
        if matched.empty:
            continue
        matched = matched.copy()
        matched["entries"] = matched["entries"].clip(lower=0)
        matched["exits"] = matched["exits"].clip(lower=0)
        matched_chunks.append(matched)
    if not matched_chunks:
        return pd.DataFrame(columns=usecols)
    return pd.concat(matched_chunks, ignore_index=True)


def _count_data_rows(path: str) -> int:
    """Cheap line count (header excluded), used only for the progress
    print in the train_operations loop below - reads the gzip stream
    as raw lines, never materializes the file as a DataFrame, so it
    doesn't defeat the point of the chunked-loading OOM fix."""
    with gzip.open(path, "rb") as f:
        return sum(1 for _ in f) - 1

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

def _load_csvs(dataset_dir: str) -> dict[str, object]:
    # Gzipped (.csv.gz) to stay under GitHub's 100MB per-file push limit -
    # pandas infers the compression from the ".gz" extension on its own,
    # so pd.read_csv below needs no other change.
    #
    # OOM fix: stations.csv.gz and trains.csv.gz are tiny (well under a
    # MB) and are still loaded fully with pd.read_csv, same as before -
    # that part was never the problem. passenger_flow.csv.gz and
    # train_operations.csv.gz are the two large (multi-hundred-thousand
    # row) files that caused Render Free (512MB) to OOM when all 4 CSVs
    # were read into memory simultaneously. Those two are now returned
    # as file PATHS instead of DataFrames - seed() streams each of them
    # in bounded chunks (see _derive_station_capacities_chunked,
    # _first_day_per_station, _collect_first_day_flow_rows, and the
    # train_operations loop below), so neither is ever fully
    # materialized in RAM, and the two are never held in memory at the
    # same time either.
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
    return {
        "stations": pd.read_csv(paths["stations"]),
        "trains": pd.read_csv(paths["trains"]),
        "passenger_flow_path": paths["passenger_flow"],
        "train_operations_path": paths["train_operations"],
    }

def seed(dataset_dir: str = DEFAULT_DATASET_DIR, reset: bool = False) -> None:
    create_tables()
    db = SessionLocal()

    try:
        if reset:
            print("--reset: clearing existing station/line/train/schedule/crowd data...")
                                                                         
            db.execute(text(
                "TRUNCATE TABLE journeys, predictions, train_locations, alerts, "
                "crowd_logs, station_crowd_state, train_schedule_history, "
                "train_schedules, line_stations, metro_lines, stations, trains "
                "RESTART IDENTITY CASCADE"
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

        # OOM fix: streams passenger_flow.csv.gz in bounded chunks
        # instead of loading it whole - see
        # _derive_station_capacities_chunked() for the equivalence
        # note.
        capacities = _derive_station_capacities_chunked(raw["passenger_flow_path"])

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
                capacity=capacities.get(row["station_id"], DEFAULT_CAPACITY),
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

        db.commit()
        print(f"  stations/lines/trains: {len(station_list)} stations, "
              f"{len(line_list)} lines, {len(train_list)} trains committed.")

        # OOM fix: train_operations.csv.gz is streamed in bounded
        # chunks (only the columns actually used below are read - see
        # TRAIN_OPS_USECOLS) instead of being loaded and sorted as one
        # whole DataFrame.
        #
        # The original code relied on a full
        # ops_df.sort_values(["train_id", "station_id",
        # "scheduled_arrival"]) so that, walking rows in that order,
        # "last write wins" left each (train, station, day_type)
        # timetable slot holding its chronologically most recent
        # occurrence. Sorting the whole file would require holding it
        # entirely in memory again, defeating the point of chunking.
        # Instead, timetable_slot_arrival below tracks the latest
        # scheduled_arrival seen so far per slot and only overwrites
        # timetable_by_slot when a row's arrival is >= that value -
        # an order-independent equivalent that keeps the same
        # "chronologically most recent occurrence wins" result
        # regardless of chunk/row order (verified against this
        # dataset while building this fix). Full-granularity history
        # rows are unaffected: they're all inserted regardless of
        # order, so streaming them in file order instead of sorted
        # order changes nothing about which rows end up in the table,
        # only their incidental physical insertion order.
        TRAIN_OPS_USECOLS = [
            "trip_id", "train_id", "station_id", "station_sequence",
            "scheduled_arrival", "scheduled_departure", "actual_arrival",
            "actual_departure", "delay_arrival_min", "delay_departure_min",
            "passenger_density", "weather", "delay_reason",
        ]
        total_ops_rows = _count_data_rows(raw["train_operations_path"])

        CHUNK_SIZE = 5000
        history_dicts: list[dict] = []
        timetable_by_slot: dict[tuple[int, int, DayType], dict] = {}
        timetable_slot_arrival: dict[tuple[int, int, DayType], datetime] = {}
        dropped = 0
        history_inserted = 0
        for ops_chunk in pd.read_csv(
            raw["train_operations_path"], usecols=TRAIN_OPS_USECOLS, chunksize=CSV_CHUNK_SIZE
        ):
            ops_chunk = ops_chunk.copy()
            ops_chunk["station_id"] = ops_chunk["station_id"].astype(str).str.strip()
            ops_chunk["train_id"] = ops_chunk["train_id"].astype(str).str.strip()
            ops_chunk["delay_reason"] = ops_chunk["delay_reason"].fillna("None")
            ops_chunk["delay_arrival_min"] = ops_chunk["delay_arrival_min"].fillna(0).clip(lower=0)
            ops_chunk["scheduled_arrival"] = pd.to_datetime(ops_chunk["scheduled_arrival"])
            ops_chunk["scheduled_departure"] = pd.to_datetime(ops_chunk["scheduled_departure"])
            ops_chunk["actual_arrival"] = pd.to_datetime(ops_chunk["actual_arrival"], errors="coerce")
            ops_chunk["actual_departure"] = pd.to_datetime(ops_chunk["actual_departure"], errors="coerce")

            for _, orow in ops_chunk.iterrows():
                train = train_by_number.get(orow["train_id"])
                db_station = station_rows.get(orow["station_id"])
                if not train or not db_station:
                    dropped += 1
                    continue

                arrival_dt = orow["scheduled_arrival"]
                departure_dt = orow["scheduled_departure"]
                is_weekend = arrival_dt.weekday() >= 5
                hour = arrival_dt.hour
                is_peak = 8 <= hour <= 11 or 17 <= hour <= 20
                day_type = DayType.WEEKEND if is_weekend else DayType.WEEKDAY
                delay_arrival = float(orow["delay_arrival_min"])
                delay_departure = float(orow.get("delay_departure_min", 0) or 0)
                station_sequence = int(orow["station_sequence"])

                # 1. Full-granularity history row - always inserted.
                history_dicts.append({
                    "trip_id": str(orow["trip_id"]),
                    "train_id": train.id,
                    "station_id": db_station.id,
                    "service_date": arrival_dt.date(),
                    "station_sequence": station_sequence,
                    "scheduled_arrival": arrival_dt.time(),
                    "scheduled_departure": departure_dt.time(),
                    "actual_arrival": orow["actual_arrival"].time() if pd.notna(orow["actual_arrival"]) else None,
                    "actual_departure": orow["actual_departure"].time() if pd.notna(orow["actual_departure"]) else None,
                    "delay_arrival_min": delay_arrival,
                    "delay_departure_min": delay_departure,
                    "passenger_density": orow.get("passenger_density") or None,
                    "weather": orow.get("weather") or None,
                    "delay_reason": orow["delay_reason"] if orow["delay_reason"] != "None" else None,
                })
                if len(history_dicts) >= CHUNK_SIZE:
                    db.bulk_insert_mappings(TrainScheduleHistory, history_dicts)
                    db.commit()
                    history_inserted += len(history_dicts)
                    print(f"  train_schedule_history: {history_inserted}/{total_ops_rows - dropped} inserted...", end="\r")
                    history_dicts = []

                # 2. Canonical timetable slot - keep whichever
                # occurrence has the chronologically latest
                # scheduled_arrival for this slot (see note above).
                slot_key = (train.id, db_station.id, day_type)
                if slot_key not in timetable_slot_arrival or arrival_dt >= timetable_slot_arrival[slot_key]:
                    timetable_slot_arrival[slot_key] = arrival_dt
                    delay_minutes = int(round(delay_arrival))
                    timetable_by_slot[slot_key] = {
                        "train_id": train.id,
                        "station_id": db_station.id,
                        "arrival_time": arrival_dt.time(),
                        "departure_time": departure_dt.time(),
                        "platform_number": (station_sequence % 2) + 1,
                        "station_sequence": station_sequence,
                        "day_type": day_type,
                        "is_peak_hour": bool(is_peak),
                        "frequency_minutes": 5 if is_peak else 12,
                        "delay_minutes": delay_minutes,
                        "status": ScheduleStatus.DELAYED if delay_minutes > 0 else ScheduleStatus.ON_TIME,
                    }
        if history_dicts:
            db.bulk_insert_mappings(TrainScheduleHistory, history_dicts)
            db.commit()
            history_inserted += len(history_dicts)
        if dropped:
            print(f"\ntrain_operations: dropped {dropped} row(s) with an unknown station_id/train_id")
        print(f"  train_schedule_history: {history_inserted} inserted (done).")

        timetable_dicts = list(timetable_by_slot.values())
        for i in range(0, len(timetable_dicts), CHUNK_SIZE):
            db.bulk_insert_mappings(TrainSchedule, timetable_dicts[i:i + CHUNK_SIZE])
            db.commit()
        print(f"  train_schedules: {len(timetable_dicts)} canonical slots inserted "
              f"(collapsed from {history_inserted} historical rows).")

        # Phase 3 fix (Bug 3): both entries here are now built by
        # _build_seed_crowd_rows() from a REAL, correctly-computed
        # net-flow occupancy walk (entries - exits, clamped at 0) over
        # each station's actual first calendar day of passenger_flow
        # rows - not `entries + exits` averaged across the whole
        # dataset (which was the same throughput-as-occupancy bug as
        # Bug 1, just in the seed script instead of the simulator).
        # station_crowd_state is now seeded too, so the live
        # dashboard/heatmap have real data immediately instead of
        # being empty until the simulator's first tick.
        # OOM fix: streams passenger_flow.csv.gz in two bounded-memory
        # passes (first to find each station's earliest calendar day,
        # then to collect just that day's rows) instead of loading the
        # whole file - see _first_day_per_station() and
        # _collect_first_day_flow_rows() for the equivalence note.
        # _build_seed_crowd_rows() itself is unchanged.
        first_day = _first_day_per_station(raw["passenger_flow_path"])
        flow_df = _collect_first_day_flow_rows(raw["passenger_flow_path"], first_day)

        crowd_rows, live_state_rows = _build_seed_crowd_rows(flow_df, station_rows)
        db.add_all(crowd_rows)
        db.flush()
        if live_state_rows:
            db.bulk_insert_mappings(StationCrowdState, live_state_rows)

        db.commit()
        print(
            f"Seeded {len(station_rows)} real stations across "
            f"{stations_df['city'].nunique()} cities, {len(line_by_key)} lines, "
            f"{len(train_by_number)} trains (real capacity + commissioned_date "
            f"from trains.csv), {len(timetable_dicts)} canonical timetable slots, "
            f"{history_inserted} historical schedule records, "
            f"{len(crowd_rows)} historical crowd_logs rows (real first-day "
            f"net-occupancy walk per station), and {len(live_state_rows)} live "
            f"station_crowd_state rows - EVERY station now has real "
            f"passenger_flow.csv coverage (this dataset covers all "
            f"{len(station_rows)}, not just 6 like the previous one) and a real, "
            f"non-hardcoded capacity derived from its own crowding_index."
        )
    finally:
        db.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=DEFAULT_DATASET_DIR, help="Folder containing the 4 CSVs")
    parser.add_argument("--reset", action="store_true", help="Wipe existing station/line/schedule/crowd data first")
    args = parser.parse_args()
    seed(dataset_dir=args.dir, reset=args.reset)