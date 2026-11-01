from __future__ import annotations

import asyncio
import os
import random
import time
from collections import deque
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core import cache
from app.core.config import settings
from app.enums.crowd_level import CrowdLevel
from app.enums.journey_status import JourneyStatus
from app.enums.notification_source import NotificationSource
from app.models.crowd_log import CrowdLog
from app.models.journey import Journey
from app.models.station import Station
from app.models.station_crowd_state import StationCrowdState
from app.services import notification_service
from app.utils.geo import state_for_city
from app.utils.timezone import to_business_time
from app.websocket.events import CROWD_UPDATE
from app.websocket.manager import manager

CSV_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "datasets", "source", "passenger_flow.csv.gz"
)

COL_STATION_ID = "station_id"                                              
COL_TIMESTAMP = "timestamp"
COL_ENTRIES = "entries"
COL_EXITS = "exits"
COL_CROWD_LABEL = "crowding_label"

_CSV_USECOLS = [COL_STATION_ID, COL_TIMESTAMP, COL_ENTRIES, COL_EXITS, COL_CROWD_LABEL]

_CSV_CHUNKSIZE = 100_000

_STATION_BUFFER_ROWS = 64

_LABEL_TO_LEVEL = {
    "low": CrowdLevel.LOW,
    "normal": CrowdLevel.MODERATE,
    "crowded": CrowdLevel.HIGH,
    "critically overcrowded": CrowdLevel.CRITICAL,
}

_rows_by_station: dict[str, deque] = {}

_cursor: dict[str, int] = {}

_STATION_INDEX: dict[str, dict] = {}
_loaded = False

_occupancy_by_station: dict[str, float] = {}
_last_row_date_by_station: dict[str, object] = {}

_stations_cache: list[dict] | None = None
_stations_cache_at: float = 0.0
                                                                      
_STATIONS_CACHE_TTL_SECONDS = 600

_last_critical_notified_at: dict[int, float] = {}
CRITICAL_NOTIFY_COOLDOWN_SECONDS = 900
NOTIFIABLE_CROWD_LEVELS = {CrowdLevel.HIGH, CrowdLevel.CRITICAL}

_last_history_written_at: dict[int, float] = {}

_REPLAY_STATE_CACHE_KEY = "simulator:crowd_replay_state"
_REPLAY_STATE_TTL_SECONDS = 3600

def _load_stations(db: Session) -> list[dict]:
    global _stations_cache, _stations_cache_at
    now = time.monotonic()
    if _stations_cache is not None and (now - _stations_cache_at) < _STATIONS_CACHE_TTL_SECONDS:
        return _stations_cache

    rows = db.query(Station).filter(Station.is_active.is_(True)).all()
    _stations_cache = [
        {
            "id": s.id,
            "station_code": s.station_code,
            "station_name": s.station_name,
            "capacity": s.capacity,
            "city": s.city,
        }
        for s in rows
    ]
    _stations_cache_at = now
    return _stations_cache

def _load_csv_once() -> None:
    global _loaded
    if _loaded:
        return
    if not os.path.exists(CSV_PATH):
        print(f"[csv_replay] {CSV_PATH} not found — live replay disabled, "
              f"no fabricated data will be shown for uncovered stations.")
        _loaded = True
        return

    global_offset = 0
    reader = pd.read_csv(
        CSV_PATH,
        usecols=_CSV_USECOLS,
        parse_dates=[COL_TIMESTAMP],
        chunksize=_CSV_CHUNKSIZE,
    )
    for chunk in reader:
        chunk[COL_STATION_ID] = chunk[COL_STATION_ID].astype(str).str.strip()
        arr = chunk[COL_STATION_ID].to_numpy()
        if len(arr):

            change_points = np.where(arr[1:] != arr[:-1])[0] + 1
            run_starts = np.concatenate(([0], change_points))
            run_ends = np.concatenate((change_points, [len(arr)]))
            for start, end in zip(run_starts, run_ends):
                station_id = arr[start]
                length = int(end - start)
                info = _STATION_INDEX.get(station_id)
                if info is None:
                    _STATION_INDEX[station_id] = {"start": global_offset + int(start), "count": length}
                    buf = _rows_by_station.setdefault(station_id, deque())
                    _cursor.setdefault(station_id, 0)
                else:
                    info["count"] += length
                    buf = _rows_by_station[station_id]

                if len(buf) < _STATION_BUFFER_ROWS:
                    room = _STATION_BUFFER_ROWS - len(buf)

                    for rec in chunk.iloc[start:start + room].to_dict(orient="records"):
                        buf.append(rec)
                    _cursor[station_id] += min(room, length)
        global_offset += len(arr)

        del chunk, arr

    _loaded = True
    print(f"[csv_replay] indexed {len(_STATION_INDEX)} station(s), {global_offset} real rows, "
          f"from {os.path.basename(CSV_PATH)} - buffering up to {_STATION_BUFFER_ROWS} rows per "
          f"station at a time, never the full dataset.")

    _restore_replay_state()

def _restore_replay_state() -> None:
    saved = cache.get_json(_REPLAY_STATE_CACHE_KEY)
    if not saved:
        return
    restored = 0
    for station_code, state in saved.items():
        if station_code not in _STATION_INDEX or not isinstance(state, dict):
            continue

        occupancy = state.get("occupancy")
        if isinstance(occupancy, (int, float)):
            _occupancy_by_station[station_code] = float(occupancy)

        last_row_date = state.get("last_row_date")
        if last_row_date:
            try:
                _last_row_date_by_station[station_code] = date.fromisoformat(last_row_date)
            except (TypeError, ValueError):
                pass

        restored += 1

    if restored:
        print(f"[csv_replay] resumed occupancy state for {restored} station(s) "
              f"from the previous leader (leader-failover continuity).")

def _persist_replay_state() -> None:
    if not _STATION_INDEX:
        return
    state = {
        station_code: {
            "occupancy": _occupancy_by_station.get(station_code, 0.0),
            "last_row_date": (
                _last_row_date_by_station[station_code].isoformat()
                if isinstance(_last_row_date_by_station.get(station_code), date)
                else None
            ),
        }
        for station_code in _STATION_INDEX
    }
    cache.set_json(_REPLAY_STATE_CACHE_KEY, state, ttl_seconds=_REPLAY_STATE_TTL_SECONDS)

def _refill_station_buffer(station_code: str) -> bool:
    info = _STATION_INDEX.get(station_code)
    if not info or info["count"] <= 0:
        return False

    cursor = _cursor.get(station_code, 0)
    if cursor >= info["count"]:
        cursor = 0
    take = min(_STATION_BUFFER_ROWS, info["count"] - cursor)
    skip_data_rows = info["start"] + cursor

    window = pd.read_csv(
        CSV_PATH,
        usecols=_CSV_USECOLS,
        parse_dates=[COL_TIMESTAMP],
        skiprows=range(1, skip_data_rows + 1),
        nrows=take,
    )
    buf = _rows_by_station.setdefault(station_code, deque())

    for rec in window.to_dict(orient="records"):
        buf.append(rec)
    _cursor[station_code] = cursor + len(window)

    del window
    return True

def _next_row(station_code: str) -> dict | None:
    buf = _rows_by_station.get(station_code)
    if buf is None:
        return None
    if not buf:
        _refill_station_buffer(station_code)
        buf = _rows_by_station.get(station_code)
    if not buf:
        return None
    return buf.popleft()

def _current_count_from_row(station_code: str, row: dict) -> int:
    entries = float(row[COL_ENTRIES]) if COL_ENTRIES else 0.0
    exits = float(row[COL_EXITS]) if COL_EXITS else 0.0

    row_date = row[COL_TIMESTAMP].date() if hasattr(row[COL_TIMESTAMP], "date") else None
    last_date = _last_row_date_by_station.get(station_code)
    if last_date is None or row_date != last_date:
        _occupancy_by_station[station_code] = 0.0
    _last_row_date_by_station[station_code] = row_date

    new_occupancy = max(0.0, _occupancy_by_station.get(station_code, 0.0) + entries - exits)
    _occupancy_by_station[station_code] = new_occupancy
    return int(round(new_occupancy))

def _level_from_row(row: dict, ratio: float) -> CrowdLevel:
    return CrowdLevel.from_ratio(ratio)

def _synthetic_count(station: dict, now_dt: datetime) -> int:
    local_dt = to_business_time(now_dt)
    hour = local_dt.hour + local_dt.minute / 60
    morning_peak = np.exp(-((hour - 9) ** 2) / 4)
    evening_peak = np.exp(-((hour - 18.5) ** 2) / 5)
    base_ratio = 0.12 + 0.55 * morning_peak + 0.6 * evening_peak
    if local_dt.weekday() >= 5:
        base_ratio *= 0.55
    base_ratio *= random.uniform(0.9, 1.1)

    capacity = station["capacity"] or 0
    return max(0, round(capacity * base_ratio))

def _active_checkins_by_station_by_station(db: Session, station_ids: list[int]) -> dict[int, int]:
    if not station_ids:
        return {}
    rows = (
        db.query(Journey.source_station_id, func.count(Journey.id))
        .filter(
            Journey.source_station_id.in_(station_ids),
            Journey.status == JourneyStatus.ACTIVE,
        )
        .group_by(Journey.source_station_id)
        .all()
    )
    return {station_id: count for station_id, count in rows}

def _upsert_live_state(db: Session, rows: list[dict]) -> None:
    if not rows:
        return
    stmt = pg_insert(StationCrowdState).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[StationCrowdState.station_id],
        set_={
            "current_count": stmt.excluded.current_count,
            "crowd_level": stmt.excluded.crowd_level,
            "updated_at": func.now(),
        },
    )
    db.execute(stmt)

def _should_write_history(station_id: int, now: float) -> bool:
    interval = settings.CROWD_HISTORY_INTERVAL_SECONDS
    if interval <= 0:
        return True
    last = _last_history_written_at.get(station_id, 0.0)
    if now - last >= interval:
        _last_history_written_at[station_id] = now
        return True
    return False

def _tick_sync(db: Session) -> list[dict]:
    _load_csv_once()
    if not _rows_by_station:
        return []

    stations = _load_stations(db)
    stations_by_id_lookup = {s["id"]: s for s in stations}
    checked_in_by_station = _active_checkins_by_station_by_station(db, [s["id"] for s in stations])
    updates: list[dict] = []
    live_state_rows: list[dict] = []
    history_logs: list[CrowdLog] = []
    now = time.monotonic()

    for station in stations:
        row = _next_row(station["station_code"])
        if row is None:

            base_count = _synthetic_count(station, datetime.now(timezone.utc))
            source_timestamp = datetime.now(timezone.utc).isoformat()
        else:
            base_count = _current_count_from_row(station["station_code"], row)
            source_timestamp = str(row[COL_TIMESTAMP])

        checked_in = checked_in_by_station.get(station["id"], 0)
        count = base_count + checked_in                                                 

        capacity = station["capacity"]
        ratio = count / capacity if capacity else 0
        level = CrowdLevel.from_ratio(ratio) if row is None else _level_from_row(row, ratio)

        live_state_rows.append({
            "station_id": station["id"],
            "current_count": count,
            "crowd_level": level,
        })

        if _should_write_history(station["id"], now):
            history_logs.append(CrowdLog(
                station_id=station["id"],
                current_count=count,
                crowd_level=level,
            ))

        updates.append({
            "station_id": station["id"],
            "station_code": station["station_code"],
            "station_name": station["station_name"],
            "current_count": count,
            "crowd_level": level,
            "source_timestamp": source_timestamp,
        })

        if level in NOTIFIABLE_CROWD_LEVELS:
            now_mono = time.monotonic()
            last_notified = _last_critical_notified_at.get(station["id"], 0.0)
            if now_mono - last_notified >= CRITICAL_NOTIFY_COOLDOWN_SECONDS:
                _last_critical_notified_at[station["id"]] = now_mono
                is_critical = level == CrowdLevel.CRITICAL
                severity_phrase = "critically overcrowded" if is_critical else "experiencing high crowding"
                notification_service.create_notification(
                    db,
                    source=NotificationSource.SYSTEM,
                    title=f"{'Critical' if is_critical else 'High'} crowding - {station['station_name']}",
                    message=(
                        f"{station['station_name']} is {severity_phrase} "
                        f"({count}/{capacity or 'unknown capacity'} passengers)."
                    ),
                    state=station["city"] if station.get("city") else None,
                )
        else:
            _last_critical_notified_at.pop(station["id"], None)

    _upsert_live_state(db, live_state_rows)
    if history_logs:
        db.add_all(history_logs)

    if updates:
        db.commit()
        cache.delete("crowd:dashboard:all")
        touched_states: set[str] = set()
        touched_cities: set[str] = set()
        for row in updates:
            cache.delete(f"crowd:latest:{row['station_id']}")
            station = stations_by_id_lookup.get(row["station_id"])
            city = station["city"] if station else None
            if city:
                touched_cities.add(city)
                state = state_for_city(city)
                if state:
                    touched_states.add(state)
        for state in touched_states:
            cache.delete(f"crowd:dashboard:{state}")
        for city in touched_cities:
            cache.delete(f"crowd:dashboard:{city}")

    _persist_replay_state()

    return updates

async def replay_tick(db: Session) -> list[dict]:
    updates = await asyncio.to_thread(_tick_sync, db)
    if updates:

        await manager.broadcast_everywhere(
            CROWD_UPDATE,
            {"updates": updates, "timestamp": datetime.now(timezone.utc).isoformat()},
        )
    return updates

async def run_forever(
    session_factory, interval_seconds: int = 60, *, control_provider=None
) -> None:
    while True:
        control = control_provider() if control_provider else None
        if control is not None:
            if not control.get("crowd_enabled", False):
                await asyncio.sleep(2)
                continue
            interval_seconds = int(control.get("crowd_interval_seconds", interval_seconds))
        db = session_factory()
        try:
            await replay_tick(db)
        except asyncio.CancelledError:

            raise
        except Exception as exc:
            print(f"[csv_replay] tick failed, will retry next interval: {exc}")

            db.rollback()
            db.close()
        else:
            db.close()
        if control_provider:
            from app.simulator.control import wait_for_controlled_tick

            await wait_for_controlled_tick("crowd", interval_seconds, control_provider)
        else:
            await asyncio.sleep(interval_seconds)
