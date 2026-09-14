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
# Gzipped to stay under GitHub's 100MB file limit; pd.read_csv below
# infers the compression from the ".gz" extension automatically.
COL_STATION_ID = "station_id"                                              
COL_TIMESTAMP = "timestamp"
COL_ENTRIES = "entries"
COL_EXITS = "exits"
COL_CROWD_LABEL = "crowding_label"

# Memory fix: the CSV has 15 columns (city, station_name, line,
# weather, crowding_index, ... ) but the replay below only ever reads
# these 5. Every read of the file (the startup index pass and every
# later per-station refill) passes usecols=_CSV_USECOLS so the other
# 10 columns are never parsed or held in memory at all.
_CSV_USECOLS = [COL_STATION_ID, COL_TIMESTAMP, COL_ENTRIES, COL_EXITS, COL_CROWD_LABEL]
# Rows pulled into memory per read_csv chunk during the one-time
# startup scan. Bounds peak memory during that scan to one chunk at a
# time - it is never used to materialize the whole file at once.
_CSV_CHUNKSIZE = 100_000
# How many rows of REAL data each station keeps buffered in RAM at
# once. This is the actual memory fix: instead of holding every
# station's full CSV history (or the full file grouped by station) in
# memory for the lifetime of the process, each station gets a small
# bounded window. When a station's buffer drains, `_refill_station_buffer`
# reads just the next `_STATION_BUFFER_ROWS` rows for THAT station off
# disk and discards the rest - so memory stays O(num_stations x
# _STATION_BUFFER_ROWS) forever, never O(num_rows_in_csv), no matter
# how large passenger_flow.csv.gz grows.
_STATION_BUFFER_ROWS = 64

_LABEL_TO_LEVEL = {
    "low": CrowdLevel.LOW,
    "normal": CrowdLevel.MODERATE,
    "crowded": CrowdLevel.HIGH,
    "critically overcrowded": CrowdLevel.CRITICAL,
}

# Bounded per-station row buffers (real CSV rows, capped at
# _STATION_BUFFER_ROWS - NOT that station's full history and NOT the
# full dataset). Populated lazily/on demand by _refill_station_buffer.
_rows_by_station: dict[str, deque] = {}
# How many rows of its own block each station has read off disk so
# far (used only to compute the next on-disk offset to resume from -
# it is an int per station, not row data, so it stays cheap even for
# a much larger dataset).
_cursor: dict[str, int] = {}
# Per-station {"start": <0-based offset of this station's first row in
# the CSV data rows>, "count": <how many rows that station has total>}.
# Built once at startup while streaming the file in bounded chunks
# (see _load_csv_once) and is the only thing that lets a later refill
# seek straight to a station's own rows without ever loading or
# grouping the full file.
_STATION_INDEX: dict[str, dict] = {}
_loaded = False

# Phase 3 fix (docs/crowd-data-correctness.md, Bug 1) — running net-flow
# occupancy accumulator, keyed per real station_id (the CSV key, not
# the DB int id, since this state is built while walking each
# station's own CSV row cursor). `entries`/`exits` in this dataset are
# HOURLY THROUGHPUT (how many people passed through the gates that
# hour - verified to hit 30,000+ in a single station-hour), not a
# point-in-time occupancy reading, so `entries + exits` was never a
# valid occupancy number - it's the volume of people who moved through
# the station, most of whom promptly left again. Real occupancy has to
# be accumulated: people who entered and haven't exited yet.
_occupancy_by_station: dict[str, float] = {}
_last_row_date_by_station: dict[str, object] = {}

_stations_cache: list[dict] | None = None
_stations_cache_at: float = 0.0
                                                                      
_STATIONS_CACHE_TTL_SECONDS = 600

# Overcrowding -> Notification Center bridge. A station can sit at
# HIGH/CRITICAL for many ticks in a row (this replay runs every few
# seconds), so this isn't "notify every tick" - it's "notify once per
# cooldown window per station", reset as soon as the station drops
# back below HIGH so the next crowded spell notifies promptly again
# instead of inheriting an old cooldown.
#
# Only HIGH and CRITICAL raise a notification - MODERATE and LOW never
# do, by design, since those are normal/expected occupancy and not
# something worth interrupting anyone's bell feed for.
_last_critical_notified_at: dict[int, float] = {}
CRITICAL_NOTIFY_COOLDOWN_SECONDS = 900
NOTIFIABLE_CROWD_LEVELS = {CrowdLevel.HIGH, CrowdLevel.CRITICAL}

# Phase 2 — write-side sampling gate for the HISTORICAL table
# (crowd_logs). The LIVE table (station_crowd_state) is always
# upserted every tick regardless of this gate - only the append-only
# historical row is throttled, since that's the table whose size
# scales with elapsed time instead of station count. Keyed per-station
# so a slow deploy that only just started replaying doesn't wait a
# full interval before its first history row either (defaults to 0.0
# => first tick for a station always writes history).
_last_history_written_at: dict[int, float] = {}

# Leader-failover replay-state continuity. `_occupancy_by_station`/
# `_last_row_date_by_station` above are per-PROCESS memory - a brand
# new process (which is exactly what a newly-elected leader is, see
# app/simulator/leader_election.py) starts them at their fresh
# defaults (occupancy 0.0), NOT wherever the previous leader left off.
# Left unaddressed, every station's live occupancy count drops at the
# exact moment failover is supposed to look seamless.
#
# Fixed the same way app/core/cache.py already caches other hot state:
# every tick, snapshot every station's {occupancy, last_row_date} as
# one JSON blob in Redis (see _persist_replay_state); a newly-started
# process loads that snapshot once, right after it builds
# `_STATION_INDEX` (see _restore_replay_state), and resumes the
# occupancy accumulator from there instead of 0.0. Same fail-open
# contract as the rest of app/core/cache.py: if Redis has nothing yet
# (first-ever boot) or is unreachable, this is a no-op and replay
# simply starts from today's existing defaults - it never blocks or
# errors the tick loop.
#
# Note: this deliberately does NOT persist a per-station CSV row
# cursor any more. The memory fix below means a new leader keeps only
# a small on-disk-backed row buffer per station (see
# _STATION_BUFFER_ROWS), not each station's full history, so there is
# no in-memory "row index into the complete dataset" to snapshot -
# only the occupancy/date accumulators, which is what actually matters
# for the dashboard staying visually continuous across a failover.
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
    """Runs exactly once per process. Builds a small per-station INDEX
    (0-based start offset + row count in the CSV - two ints per
    station, e.g. 324 stations here) and pre-fills each station's
    bounded row buffer (up to `_STATION_BUFFER_ROWS` real rows), then
    stops. It never holds the complete dataset, and never groups the
    complete dataset by station, in memory.

    MEMORY FIX: this used to (a) `pd.read_csv(CSV_PATH)` the whole
    file with no `usecols`/`chunksize`, and even after an earlier
    attempted fix, still ended up (b) grouping every chunk by station
    and concatenating those groups into one full-history DataFrame per
    station in `_rows_by_station` - i.e. the ENTIRE file, just
    reorganized, sitting in RAM for the life of the process. Real fix:
      - `usecols=_CSV_USECOLS` - only the 5 columns this replay reads
        are ever parsed; the other 10 columns in the file never enter
        memory.
      - `chunksize=_CSV_CHUNKSIZE` - this one-time startup scan streams
        the file in bounded chunks; each chunk is processed and then
        explicitly released (`del chunk, arr`) - at no point does a
        DataFrame holding the full file exist.
      - Per station, only up to `_STATION_BUFFER_ROWS` rows are ever
        kept (`_rows_by_station[station_id]` is a bounded deque, not
        that station's full history) - the rest of a station's rows
        stay on disk and are fetched later, in the same small bounded
        windows, by `_refill_station_buffer` as that station's buffer
        drains during replay. Real dataset rows are still replayed in
        their real recorded order; this only changes how much of that
        real history sits in memory at once.
    """
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
            # Contiguous-run boundaries within this chunk (a station's
            # rows are recorded as one contiguous block in this
            # dataset) - cheap, vectorized, and only ever produces a
            # handful of runs per chunk, never a per-row Python loop
            # over the chunk.
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
                # Only ever buffer up to _STATION_BUFFER_ROWS rows per
                # station here - a station whose real block is much
                # longer than that simply leaves the remainder on disk,
                # to be pulled in later (bounded, on demand) as this
                # buffer drains during replay.
                if len(buf) < _STATION_BUFFER_ROWS:
                    room = _STATION_BUFFER_ROWS - len(buf)
                    # MEMORY FIX: .iterrows() used to yield one pd.Series
                    # per buffered row - each Series carries its own Index/
                    # block-manager/dtype overhead (measured ~5KB deep size
                    # per row vs. the ~5 scalar values it actually holds),
                    # which added up to ~25-40MB of pure per-row object
                    # overhead across the ~23k rows this buffer holds
                    # (370 stations x up to 64 rows). to_dict(orient=
                    # "records") produces one plain dict per row instead -
                    # same values, same dict[column]-style access every
                    # downstream reader (_current_count_from_row,
                    # _level_from_row, etc.) already uses, at a fraction of
                    # the memory.
                    for rec in chunk.iloc[start:start + room].to_dict(orient="records"):
                        buf.append(rec)
                    _cursor[station_id] += min(room, length)
        global_offset += len(arr)
        # Explicitly release this chunk before pulling the next one -
        # only the bounded per-station buffers above and the tiny
        # {start, count} index survive past this loop iteration.
        del chunk, arr

    _loaded = True
    print(f"[csv_replay] indexed {len(_STATION_INDEX)} station(s), {global_offset} real rows, "
          f"from {os.path.basename(CSV_PATH)} - buffering up to {_STATION_BUFFER_ROWS} rows per "
          f"station at a time, never the full dataset.")

    # Resume the occupancy/date accumulators from wherever the
    # previous leader left off, if anything was persisted (see
    # _REPLAY_STATE_CACHE_KEY's docstring above) - runs exactly once
    # per process, right after `_STATION_INDEX` is first populated,
    # which is exactly when a newly-elected leader needs it.
    _restore_replay_state()


def _restore_replay_state() -> None:
    """Best-effort resume of the occupancy accumulator + last-row-date
    from whatever the previous leader last persisted (see
    _persist_replay_state below). No-op if nothing was ever persisted
    (first-ever boot) or Redis is unreachable - every station simply
    keeps its existing defaults (occupancy 0.0) exactly as before this
    fix; this never blocks or raises.
    """
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
    """Snapshot every station's occupancy accumulator + last-row-date
    as one JSON blob, refreshed every tick, so a NEW leader process
    (after leadership changes hands - see
    app/simulator/leader_election.py) can resume its occupancy counts
    instead of silently dropping every station back to 0. Best-effort:
    if Redis is unreachable this is simply skipped - failover itself
    doesn't depend on this (LeaderElection handles that independently),
    the new leader just falls back to starting that one station's
    occupancy fresh, same as before this fix.
    """
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
    """Reads the NEXT bounded window (up to `_STATION_BUFFER_ROWS` real
    rows) for exactly ONE station off disk, appends them to that
    station's buffer, then discards the temporary window DataFrame.
    This is the only disk read that happens after startup, and it only
    ever touches one station's own rows - never another station's,
    never the whole file.

    Wraps back to the start of that station's own block when it runs
    out (`cursor >= count`), same "loop the real recorded history"
    behavior the old per-station cursor had - just re-derived from the
    on-disk index instead of an in-memory frame length.
    """
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
    # MEMORY FIX: same fix as the initial buffer fill in _load_csv_once
    # above - to_dict(orient="records") instead of .iterrows() avoids
    # allocating one full pd.Series (Index + block-manager overhead)
    # per buffered row.
    for rec in window.to_dict(orient="records"):
        buf.append(rec)
    _cursor[station_code] = cursor + len(window)
    # Explicitly release the temporary window - only the bounded
    # buffer (`buf`, capped at _STATION_BUFFER_ROWS entries) survives.
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
    """Real net occupancy for this station, NOT throughput.

    Phase 3 fix (docs/crowd-data-correctness.md, Bug 1): the old version
    returned `entries + exits`, which is hourly throughput (verified
    to hit 30,000+ in a single station-hour on this dataset) - i.e.
    exactly the "current_count = entries + exits" anti-pattern the
    project brief warns against, since it counts everyone who passed
    through a gate that hour, not how many are still inside.

    Real occupancy is a running balance: `occupancy += entries -
    exits`, clamped at 0 (occupancy can't go negative - a station that
    reports more exits than it ever had entrants is a counting
    artifact, not -40 real people) and reset at day boundaries. The
    reset is grounded in the real data, not assumed: per-station daily
    net flow (sum of entries - exits across a whole real day) is
    tiny relative to that day's total ridership - a median of ~1.5% of
    daily entries, per station, across the full 4-month dataset (see
    docs/crowd-data-correctness.md) - confirming stations really do empty out
    by the time service resumes the next day, so restarting the
    accumulator at 0 on a new calendar date (or when a station's CSV
    history cycles back to its first row) reflects what the data
    actually shows rather than carrying rounding drift forever.

    Deliberately NOT hard-capped at `capacity`: the dataset's own
    `crowding_index` intentionally goes up to 1.5x capacity to
    represent real overcrowding, and clamping occupancy at capacity
    here would silently erase that signal before it ever reaches the
    crowd-level/alerting logic.
    """
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
    """Bugfix: this used to prefer the CSV's own `crowding_label` column
    whenever present, only falling back to `CrowdLevel.from_ratio(ratio)`
    if it was missing - and since every row in this dataset has a label,
    that fallback never actually ran. The label was authored against the
    dataset's original entries+exits-based crowding_index, which Phase 3
    (see _current_count_from_row above) established is throughput, not
    occupancy - it was already fixed there, but this function kept
    trusting the label anyway. The result: a station sitting at 36/2397
    (1.5%) real occupancy could still be tagged CRITICAL because that's
    what its row's stale label said, wildly disagreeing with the `count`
    actually shown on the card and firing bogus "critically overcrowded"
    notifications for near-empty stations.

    `ratio` here is computed from the same corrected net-occupancy
    `count` (including live check-ins) that the dashboard displays, so
    deriving level from it directly is what keeps the crowd-level badge,
    the KPIs, and the overcrowding alerts all agreeing with each other.
    The historical CSV label is intentionally unused now - see
    docs/crowd-data-correctness.md for the measurement behind this.
    """
    return CrowdLevel.from_ratio(ratio)

def _synthetic_count(station: dict, now_dt: datetime) -> int:
    """Fallback occupancy for a station whose station_code was never
    indexed from passenger_flow.csv.gz at all (`_next_row` returns
    None for it every tick - see `_STATION_INDEX`/`_load_csv_once`
    above). The CSV only covers 370 real stations; any active DB
    station outside that set (e.g. one added after the dataset was
    captured, or an interchange/"connector" entry like "Noida Sector
    51 [Conn: Blue]" that the dataset doesn't carry as its own row
    block) used to hit the `continue` below and get skipped entirely -
    never upserted into station_crowd_state, never logged - so its
    Station Analytics card stayed frozen at "0 crowd readings" forever
    even with the simulator fully running, because this replay only
    ever advances real CSV rows and never fabricated one for a station
    with zero rows to begin with.

    This intentionally does NOT touch any station that DOES have CSV
    rows (those keep replaying their real recorded history exactly as
    before) - it only fills the gap for stations the dataset has no
    coverage of, using the same morning/evening peak-hour shape the AI
    engine's own heuristic fallback uses when the trained model has
    nothing to say (see app/ai_engine/prediction/crowd_predictor.py's
    `_heuristic`), scaled to the station's own capacity with a little
    jitter so the trend looks live rather than a flat line.
    """
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

def _upsert_live_state(db: Session, rows: list[dict]) -> None:
    """One bulk UPSERT statement for every station's live crowd state,
    instead of one UPDATE/INSERT round-trip per station.

    Uses Postgres's `INSERT ... ON CONFLICT (station_id) DO UPDATE`
    (this project targets Postgres exclusively - see
    app/database/migrate_crowd_logs_index.py for the existing
    precedent of using Postgres-specific SQL directly rather than
    hiding behind a portable-but-slower N-statement loop). Table size
    stays fixed at "one row per station" forever - this call never
    grows `station_crowd_state`, it only ever changes the value of
    rows that already exist (or creates the row once, the very first
    time a station is ever seen).
    """
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
    """Write-side sampling gate for crowd_logs (historical table).

    Returns True at most once per CROWD_HISTORY_INTERVAL_SECONDS per
    station, regardless of how often the simulator tick itself runs -
    this is what turns "one crowd_logs row per station per tick" into
    "one crowd_logs row per station per sample interval" without
    touching the tick cadence the live dashboard/WebSocket rely on.
    """
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
            # No CSV coverage for this station_code at all - synthesize
            # a reading instead of skipping the station outright (see
            # _synthetic_count's docstring for why this stayed at 0
            # before).
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

        # LIVE STATE - always upserted this tick (bounded table, one
        # row per station, is what backs the dashboard/heatmap/
        # WebSocket push - see get_station_wise_snapshot()).
        live_state_rows.append({
            "station_id": station["id"],
            "current_count": count,
            "crowd_level": level,
        })

        # HISTORICAL - only staged when the sampling gate is open, so
        # crowd_logs grows at CROWD_HISTORY_INTERVAL_SECONDS
        # resolution instead of SIMULATOR_INTERVAL_SECONDS resolution.
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
        # BUGFIX (Phase 29A - stale REST reads after a live tick):
        # this loop upserts station_crowd_state directly (bypassing
        # crowd_service.log_crowd_count/journey_service's check-in/
        # check-out paths), so it never went through
        # invalidate_station_cache() the way every other crowd-writing
        # path already does. get_station_wise_snapshot()/get_latest_crowd()
        # cache their reads under crowd:dashboard:*/crowd:latest:* for
        # CACHE_TTL_SECONDS - with nothing here ever deleting those
        # keys, a dashboard widget that reacts to this tick's
        # crowd_update WebSocket push by refetching over REST (e.g.
        # KPISection's debouncedCrowdRefetch) raced the cache and very
        # often redisplayed the previous tick's numbers instead of the
        # one that just arrived, even though the WebSocket payload
        # itself was already correct. Every station in `updates` just
        # got a fresh row, so drop the unfiltered view, every distinct
        # state/city view it can affect, and each station's own
        # single-station cache entry - same keys
        # crowd_service.invalidate_station_cache() drops, just reached
        # from this tick loop instead of a request handler.
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

    # Refresh the leader-failover replay-state snapshot every tick (see
    # _REPLAY_STATE_CACHE_KEY's docstring) - cheap, best-effort, and
    # never blocks/raises even if Redis is down.
    _persist_replay_state()

    return updates

async def replay_tick(db: Session) -> list[dict]:
    updates = await asyncio.to_thread(_tick_sync, db)
    if updates:
        # Phase 4: broadcast_everywhere() (not broadcast()) - only ONE
        # process runs this loop at a time now (see leader_election.py),
        # but every process's own dashboard clients still need this
        # pushed to them without a refresh, so it's relayed cluster-wide.
        await manager.broadcast_everywhere(
            CROWD_UPDATE,
            {"updates": updates, "timestamp": datetime.now(timezone.utc).isoformat()},
        )
    return updates

async def run_forever(session_factory, interval_seconds: int = 60) -> None:
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
            # Phase 6: explicit rollback before close. _tick_sync can
            # raise after some rows were already staged (db.add/execute)
            # but before its own db.commit() - close() alone happens to
            # roll back an open transaction too, but that's an implicit
            # SQLAlchemy detail, not a guarantee this code should lean
            # on silently; being explicit here means a dirty session is
            # never handed back to the pool by accident, on this path
            # or any future refactor of it.
            db.rollback()
        finally:
            db.close()
        await asyncio.sleep(interval_seconds)