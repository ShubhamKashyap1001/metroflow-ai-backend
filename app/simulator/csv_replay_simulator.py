from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime

import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.enums.crowd_level import CrowdLevel
from app.enums.journey_status import JourneyStatus
from app.enums.notification_source import NotificationSource
from app.models.crowd_log import CrowdLog
from app.models.journey import Journey
from app.models.station import Station
from app.services import notification_service
from app.websocket.events import CROWD_UPDATE
from app.websocket.manager import manager

CSV_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "datasets", "passenger_flow.csv.gz"
)
# Gzipped to stay under GitHub's 100MB file limit; pd.read_csv below
# infers the compression from the ".gz" extension automatically.
COL_STATION_ID = "station_id"                                              
COL_TIMESTAMP = "timestamp"
COL_ENTRIES = "entries"
COL_EXITS = "exits"
COL_CROWD_LABEL = "crowding_label"                                                                          

_LABEL_TO_LEVEL = {
    "low": CrowdLevel.LOW,
    "normal": CrowdLevel.MODERATE,
    "crowded": CrowdLevel.HIGH,
    "critically overcrowded": CrowdLevel.CRITICAL,
}

_rows_by_station: dict[str, pd.DataFrame] = {}
_cursor: dict[str, int] = {}
_loaded = False

_stations_cache: list[dict] | None = None
_stations_cache_at: float = 0.0
                                                                      
_STATIONS_CACHE_TTL_SECONDS = 600

# Overcrowding -> Notification Center bridge. A station can sit at
# CRITICAL for many ticks in a row (this replay runs every few
# seconds), so this isn't "notify every tick" - it's "notify once per
# cooldown window per station", reset as soon as the station drops
# back below CRITICAL so the next critical spell notifies promptly
# again instead of inheriting an old cooldown.
_last_critical_notified_at: dict[int, float] = {}
CRITICAL_NOTIFY_COOLDOWN_SECONDS = 900

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
    """Reads the CSV exactly once per process and groups it by the
    real station_id, sorted by its own real timestamp column - this
    ordering IS the real recorded sequence of events, nothing is
    reshuffled or generated."""
    global _loaded
    if _loaded:
        return
    if not os.path.exists(CSV_PATH):
        print(f"[csv_replay] {CSV_PATH} not found — live replay disabled, "
              f"no fabricated data will be shown for uncovered stations.")
        _loaded = True
        return

    df = pd.read_csv(CSV_PATH)
    df[COL_TIMESTAMP] = pd.to_datetime(df[COL_TIMESTAMP])
    df[COL_STATION_ID] = df[COL_STATION_ID].astype(str).str.strip()
    df = df.sort_values(COL_TIMESTAMP)

    for station_id, group in df.groupby(COL_STATION_ID):
        _rows_by_station[station_id] = group.reset_index(drop=True)
        _cursor[station_id] = 0

    _loaded = True
    print(f"[csv_replay] loaded {len(df)} real rows across "
          f"{len(_rows_by_station)} station(s) from {os.path.basename(CSV_PATH)}")

def _next_row(station_code: str) -> pd.Series | None:
    frame = _rows_by_station.get(station_code)
    if frame is None or frame.empty:
        return None
    i = _cursor[station_code]
    row = frame.iloc[i]
    _cursor[station_code] = (i + 1) % len(frame)                                                  
    return row

def _current_count_from_row(row: pd.Series) -> int:
    entries = float(row[COL_ENTRIES]) if COL_ENTRIES else 0.0
    exits = float(row[COL_EXITS]) if COL_EXITS else 0.0
    return max(0, int(round(entries + exits)))

def _level_from_row(row: pd.Series, ratio: float) -> CrowdLevel:
    if COL_CROWD_LABEL and COL_CROWD_LABEL in row.index:
        mapped = _LABEL_TO_LEVEL.get(str(row[COL_CROWD_LABEL]).strip().lower())
        if mapped is not None:
            return mapped
    return CrowdLevel.from_ratio(ratio)

def _active_checkins_by_station_by_station(db: Session, station_ids: list[int]) -> dict[int, int]:
    """How many real passengers are CURRENTLY checked in at each station
    and haven't checked out yet - as ONE grouped query for every station
    in `station_ids`, instead of one `COUNT` query per station (the
    previous version ran this per-station inside the tick loop, which
    meant 324 extra queries every 5 seconds on the full dataset). Result
    is added on top of each station's CSV base value every tick, so a
    real check-in's +1 is never silently erased by the next replay tick -
    it only ever comes back down when that passenger calls POST
    /checkout (see journey_service.check_out)."""
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

def _tick_sync(db: Session) -> list[dict]:
    _load_csv_once()
    if not _rows_by_station:
        return []

    stations = _load_stations(db)
    checked_in_by_station = _active_checkins_by_station_by_station(db, [s["id"] for s in stations])
    updates: list[dict] = []

    for station in stations:
        row = _next_row(station["station_code"])
        if row is None:
            continue                                                                   

        base_count = _current_count_from_row(row)
        checked_in = checked_in_by_station.get(station["id"], 0)
        count = base_count + checked_in                                                 

        capacity = station["capacity"]
        ratio = count / capacity if capacity else 0
        level = _level_from_row(row, ratio)

        db.add(CrowdLog(
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
            "source_timestamp": str(row[COL_TIMESTAMP]),                                                           
        })

        if level == CrowdLevel.CRITICAL:
            now = time.monotonic()
            last_notified = _last_critical_notified_at.get(station["id"], 0.0)
            if now - last_notified >= CRITICAL_NOTIFY_COOLDOWN_SECONDS:
                _last_critical_notified_at[station["id"]] = now
                notification_service.create_notification(
                    db,
                    source=NotificationSource.SYSTEM,
                    title=f"Overcrowding - {station['station_name']}",
                    message=(
                        f"{station['station_name']} is critically overcrowded "
                        f"({count}/{capacity or 'unknown capacity'} passengers)."
                    ),
                    state=station["city"] if station.get("city") else None,
                )
        else:
            _last_critical_notified_at.pop(station["id"], None)

    if updates:
        db.commit()
    return updates

async def replay_tick(db: Session) -> list[dict]:
    updates = await asyncio.to_thread(_tick_sync, db)
    if updates:
        await manager.broadcast(
            CROWD_UPDATE,
            {"updates": updates, "timestamp": datetime.utcnow().isoformat()},
        )
    return updates

async def run_forever(session_factory, interval_seconds: int = 5) -> None:
    """Drop-in replacement for live_simulator.run_forever. Call this
    from scheduler.py / main.py instead, at startup - pass the SAME
    interval_seconds you pass to train_simulator.run_forever, so both
    fire together on one shared cadence."""
    while True:
        db = session_factory()
        try:
            await replay_tick(db)
        except Exception as exc:                
            print(f"[csv_replay] tick failed, will retry next interval: {exc}")
        finally:
            db.close()
        await asyncio.sleep(interval_seconds)
