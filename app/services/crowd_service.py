from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import case, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core import cache
from app.enums.crowd_level import CrowdLevel
from app.models.crowd_log import CrowdLog
from app.models.line_station import LineStation
from app.models.metro_line import MetroLine
from app.models.station import Station
from app.models.station_crowd_state import StationCrowdState
from app.schemas.crowd_log import CrowdLogCreate
from app.utils.geo import cities_for_state, state_for_city
from app.websocket.events import CROWD_UPDATE
from app.websocket.manager import manager

# BUGFIX (remaining expensive history queries): get_inflow_outflow /
# get_inflow_outflow_bulk (and get_station_monitor, which calls the
# bulk version for every active station) take a caller-supplied
# `hours` window with no upper bound. `since = now - timedelta(hours=
# hours)` with an oversized/absurd value (e.g. ?hours=87600000 - "10
# thousand years") degrades straight into "SELECT every row ever
# written", i.e. an unbounded `.all()` over the crowd_logs table in
# disguise - the same class of problem Phase 9 already fixed for
# alerts/notifications/predictions/schedules/users, just reached via a
# time filter instead of a raw limit/offset. The frontend never asks
# for more than 72h (see ReportsPanel.tsx's WINDOW_OPTIONS), so this
# cap is far above any real usage and purely a server-side backstop.
MAX_HISTORY_WINDOW_HOURS = 720  # 30 days

def _clamp_hours(hours: int) -> int:
    return min(max(hours or 1, 1), MAX_HISTORY_WINDOW_HOURS)

def invalidate_station_cache(station: Station) -> None:
    """Drop every cached view that this station's new crowd count affects.

    Called right after a crowd log commit so the next read (even one
    that lands well inside the TTL) sees the fresh count instead of a
    stale one - the TTL alone is a staleness *ceiling*, this is what
    keeps the common case (read shortly after a write) accurate too.
    Public (no leading underscore) so other services that write a
    CrowdLog directly in their own transaction - e.g. journey_service,
    which batches the crowd bump into the same commit as the journey
    row instead of going through log_crowd_count()'s separate commit -
    can invalidate the same keys without duplicating this logic.
    """
    cache.delete(f"crowd:latest:{station.id}")
    cache.delete("crowd:dashboard:all")
    state = state_for_city(station.city)
    if state:
        cache.delete(f"crowd:dashboard:{state}")
                                                                        
    cache.delete(f"crowd:dashboard:{station.city}")

def log_crowd_count(db: Session, payload: CrowdLogCreate) -> CrowdLog:
    """Ingest a manual/sensor crowd reading.

    Unlike the simulator tick (which is throttled - see
    csv_replay_simulator.py's CROWD_HISTORY_INTERVAL_SECONDS gate),
    every call here is a real, discrete external event (a
    ticketing/sensor feed posting a genuine reading), so it always
    gets both an immediate live-state upsert AND a historical row -
    no sampling gate applies to explicitly-reported data.
    """
    station = db.get(Station, payload.station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")

    ratio = payload.current_count / station.capacity if station.capacity else 0
    level = payload.crowd_level or CrowdLevel.from_ratio(ratio)

    log = CrowdLog(
        station_id=payload.station_id,
        current_count=payload.current_count,
        crowd_level=level,
    )
    db.add(log)
    upsert_live_state(db, station.id, payload.current_count, level)
    db.commit()
    db.refresh(log)
    invalidate_station_cache(station)
    broadcast_crowd_update(station, payload.current_count, level)
    return log

def upsert_live_state(db: Session, station_id: int, current_count: int, level: CrowdLevel) -> None:
    """Single-station upsert into station_crowd_state (live table).

    Used for ABSOLUTE readings (manual POST /crowd/, and the
    simulator's per-tick bulk upsert) where the caller already knows
    the authoritative new value and simply wants "last write wins" -
    NOT for delta-based updates (see apply_live_state_delta below for
    those, which need a row lock instead of a blind overwrite).
    """
    stmt = pg_insert(StationCrowdState).values(
        station_id=station_id,
        current_count=current_count,
        crowd_level=level,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[StationCrowdState.station_id],
        set_={
            "current_count": stmt.excluded.current_count,
            "crowd_level": stmt.excluded.crowd_level,
            "updated_at": func.now(),
        },
    )
    db.execute(stmt)

def apply_live_state_delta(db: Session, station: Station, delta: int) -> tuple[int, CrowdLevel]:
    """Atomically apply +1/-1 (check-in/check-out) to a station's live
    count and return the resulting (count, level).

    A naive "read current_count, add delta, write it back" (what this
    function replaces) has a lost-update race: two concurrent
    check-ins on the same station can both read the same starting
    value and each write `start + 1`, so one of the two increments is
    silently lost - verified under a 15-way concurrent check-in test
    against a real Postgres instance during this phase (see
    docs/crowd-live-state-and-retention.md).

    Fixed with `SELECT ... FOR UPDATE`: this takes a row-level lock on
    the station's `station_crowd_state` row, so a second concurrent
    call blocks until the first one's transaction commits (or rolls
    back) instead of reading a stale value - the read and the write
    happen as one atomic unit per caller. Concurrent check-ins on
    DIFFERENT stations are completely unaffected (they lock different
    rows) - only same-station contention serializes, which is exactly
    the case that needs it.
    """
    # Bootstrap the row if this is the very first time this station
    # has ever had a live-state row (ON CONFLICT DO NOTHING is itself
    # safe to race - at most one of several concurrent first-timers
    # actually inserts, the rest no-op and fall through to the SELECT
    # ... FOR UPDATE below).
    bootstrap = pg_insert(StationCrowdState).values(
        station_id=station.id,
        current_count=0,
        crowd_level=CrowdLevel.LOW,
    ).on_conflict_do_nothing(index_elements=[StationCrowdState.station_id])
    db.execute(bootstrap)

    state = (
        db.query(StationCrowdState)
        .filter(StationCrowdState.station_id == station.id)
        .with_for_update()
        .one()
    )
    new_count = max(0, state.current_count + delta)
    ratio = new_count / station.capacity if station.capacity else 0
    level = CrowdLevel.from_ratio(ratio)
    state.current_count = new_count
    state.crowd_level = level
    return new_count, level

def broadcast_crowd_update(station: Station, current_count: int, level: CrowdLevel) -> None:
    """Push a single-station crowd_update over the WebSocket, in the
    same event shape the simulator's per-tick broadcast already uses
    (a list of updates - just one entry here), so the frontend needs
    no special-casing to handle a request-triggered update vs. a
    simulator-tick update.

    Uses manager.notify() (sync, thread-safe, fire-and-forget) since
    this runs inside FastAPI's sync-route threadpool, not on the
    asyncio event loop - see websocket/manager.py's own docstring for
    why notify() vs. broadcast() matters here.
    """
    manager.notify(CROWD_UPDATE, {
        "updates": [{
            "station_id": station.id,
            "station_code": station.station_code,
            "station_name": station.station_name,
            "current_count": current_count,
            "crowd_level": level,
        }],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

def get_latest_crowd(db: Session, station_id: int) -> dict | None:
    """Live snapshot for one station, read from station_crowd_state -
    a single-row primary-key lookup instead of an ORDER BY .. LIMIT 1
    scan over the historical crowd_logs table."""
    cache_key = f"crowd:latest:{station_id}"
    cached = cache.get_json(cache_key)
    if cached is not None:
        return cached

    state = db.get(StationCrowdState, station_id)
    if state is None:
        return None

    result = {
        "id": state.station_id,
        "station_id": state.station_id,
        "current_count": state.current_count,
        "crowd_level": state.crowd_level,
        "created_at": state.updated_at,
    }
    cache.set_json(cache_key, result)
    return result

def get_station_wise_snapshot(db: Session, state: str | None = None) -> list[dict]:
                                                                          
    cache_key = f"crowd:dashboard:{state or 'all'}"
    cached = cache.get_json(cache_key)
    if cached is not None:
        return cached

    snapshot = _get_station_wise_snapshot_from_db(db, state)
    cache.set_json(cache_key, snapshot)
    return snapshot

def _get_station_wise_snapshot_from_db(db: Session, state: str | None = None) -> list[dict]:
    """Reads the dashboard/heatmap/congestion snapshot from
    station_crowd_state (live table, one row per station) instead of
    running a ROW_NUMBER() OVER (...) window query over the much
    larger, ever-growing crowd_logs table. Same output shape as
    before - this is a data-source swap, not a behaviour change."""
    # MAP FIX (crowd heatmap: unreadable pile-up of station dots/labels):
    # the heatmap/dashboard maps draw a polyline connecting every
    # station on the same metro line (see mapLines.ts on the frontend),
    # but this query never told the frontend which line a station
    # belongs to - so that feature silently did nothing and the map had
    # no structure to organise ~280+ stations by. Left-joining
    # line_stations -> metro_lines here (a station has at most one line
    # row in this schema; interchanges are modelled as one `stations`
    # row per line, see the dedupe note in get_heatmap below) gives the
    # frontend line_name/line_color/station_order so it can draw real
    # line paths and no longer has to dump every station into one
    # undifferentiated blob.
    query = (
        db.query(
            Station,
            StationCrowdState.current_count,
            StationCrowdState.crowd_level,
            StationCrowdState.updated_at,
            MetroLine.line_name,
            MetroLine.color,
            LineStation.station_order,
        )
        .outerjoin(
            StationCrowdState,
            StationCrowdState.station_id == Station.id,
        )
        .outerjoin(
            LineStation,
            LineStation.station_id == Station.id,
        )
        .outerjoin(
            MetroLine,
            MetroLine.id == LineStation.line_id,
        )
        .filter(Station.is_active.is_(True))
    )

    cities = cities_for_state(state)
    if cities:
        query = query.filter(Station.city.in_(cities))

    rows = query.all()

    snapshot = []
    for (
        station,
        current_count,
        crowd_level,
        last_updated,
        line_name,
        line_color,
        station_order,
    ) in rows:
        current_count = current_count or 0
        snapshot.append({
            "station_id": station.id,
            "station_name": station.station_name,
            "capacity": station.capacity,
            "current_count": current_count,
            "crowd_level": crowd_level or CrowdLevel.LOW,
            "occupancy_ratio": round((current_count / station.capacity), 3)
            if station.capacity else 0,
            "last_updated": last_updated,
            "latitude": station.latitude,
            "longitude": station.longitude,
            "line_name": line_name,
            "line_color": line_color,
            "station_order": station_order,
        })
    return snapshot

def get_heatmap(db: Session, state: str | None = None, limit: int | None = None) -> list[dict]:
    snapshot = get_station_wise_snapshot(db, state)
    heatmap = [
        entry for entry in snapshot
        if entry["latitude"] is not None
        and entry["longitude"] is not None
        and not (entry["latitude"] == 0 and entry["longitude"] == 0)
        # A station with no (or 0) capacity has no meaningful occupancy
        # percentage - it can only ever render as a stray "0%" dot that
        # clutters the map without telling the viewer anything. Drop it
        # from the heatmap entirely instead of plotting a useless point.
        and (entry.get("capacity") or 0) > 0
    ]

    # BUGFIX (dashboard: same station plotted twice on the heatmap):
    # interchange stations are stored as one `stations` row per metro
    # line they sit on (same station_name, different station_id, ~same
    # lat/lng) - that's correct for routing, but it means the raw
    # snapshot has two rows for e.g. one physical "Kashmere Gate", so
    # the heatmap drew two overlapping dots with the same label. Dedupe
    # by station_name here, right before the map/UI ever sees the data,
    # keeping the copy with the higher occupancy_ratio so a genuinely
    # crowded interchange never gets hidden behind its quieter twin.
    deduped: dict[str, dict] = {}
    for entry in heatmap:
        key = entry["station_name"].strip().lower()
        existing = deduped.get(key)
        if existing is None or (entry.get("occupancy_ratio") or 0) > (existing.get("occupancy_ratio") or 0):
            deduped[key] = entry
    heatmap = list(deduped.values())

    if limit is not None:
        heatmap.sort(key=lambda h: h.get("occupancy_ratio") or 0, reverse=True)
        heatmap = heatmap[:limit]
    return heatmap

def get_congested_stations(
    db: Session,
    min_level: CrowdLevel = CrowdLevel.HIGH,
    state: str | None = None,
) -> list[dict]:
    order = [CrowdLevel.LOW, CrowdLevel.MODERATE, CrowdLevel.HIGH, CrowdLevel.CRITICAL]
    threshold_index = order.index(min_level)
    snapshot = get_station_wise_snapshot(db, state)
    return [
        s for s in snapshot
        if order.index(s["crowd_level"]) >= threshold_index
    ]


def crowd_flow_aggregate(
    db: Session, station_ids: list[int], since: datetime
) -> dict[int, dict]:
    """Inflow/outflow/sample-count/last-known-count per station since
    `since`, computed ENTIRELY in SQL via window functions + GROUP BY,
    instead of pulling every raw CrowdLog row in the window into
    Python and walking it with a running-previous-count loop.

    RAM FIX (Render Free 512MB): the previous approach - `SELECT
    station_id, current_count [, created_at] ... WHERE created_at >=
    :since` then `.all()` - was already scoped by station_ids and a
    clamped time window (see MAX_HISTORY_WINDOW_HOURS above), but nothing
    bounded the *row count* returned within that window. At the
    simulator's CROWD_HISTORY_INTERVAL_SECONDS sampling rate, a
    legitimate-looking request for the maximum allowed window (30 days)
    across every active station can still mean materializing millions
    of CrowdLog rows as Python tuples in one request - exactly the
    "large query loads excessive history into Python RAM" failure mode
    this fix targets, just reached through an oversized `hours` value
    rather than a missing LIMIT.

    This query returns at most one aggregated row PER STATION (a small,
    fixed-size result - one row per station in `station_ids`) no matter
    how many raw samples exist in the window or how wide the window is;
    Postgres does the per-row delta/ordering work and only ships back
    the already-summed totals. The computed inflow/outflow/samples
    values are exactly the same numbers the old Python loop produced:
    that loop routed a delta of exactly 0 into its `else` (outflow)
    branch, contributing `abs(0) == 0` either way, which is the same
    as this query's `delta > 0` / `delta < 0` split (0 falls into
    neither `CASE`, contributing 0 to both) - only *how* the totals
    are computed changed, not *what* they mean.

    `last_count` is the most recent sample's current_count within the
    window (ROW_NUMBER() over each station ordered by created_at DESC,
    keeping rn = 1) - used by analytics_service.passenger_flow_overview
    for its occupancy KPI, which previously had to keep the last row it
    saw while looping over every raw row for the same reason.
    """
    if not station_ids:
        return {}

    ordered = (
        db.query(
            CrowdLog.station_id.label("station_id"),
            CrowdLog.current_count.label("current_count"),
            func.lag(CrowdLog.current_count)
            .over(partition_by=CrowdLog.station_id, order_by=CrowdLog.created_at.asc())
            .label("prev_count"),
            func.row_number()
            .over(partition_by=CrowdLog.station_id, order_by=CrowdLog.created_at.desc())
            .label("rn_desc"),
        )
        .filter(CrowdLog.station_id.in_(station_ids), CrowdLog.created_at >= since)
        .subquery()
    )

    delta = ordered.c.current_count - ordered.c.prev_count
    rows = (
        db.query(
            ordered.c.station_id,
            func.count().label("samples"),
            func.coalesce(func.sum(case((delta > 0, delta), else_=0)), 0).label("inflow"),
            func.coalesce(func.sum(case((delta < 0, -delta), else_=0)), 0).label("outflow"),
            func.max(
                case((ordered.c.rn_desc == 1, ordered.c.current_count), else_=None)
            ).label("last_count"),
        )
        .group_by(ordered.c.station_id)
        .all()
    )

    return {
        row.station_id: {
            "inflow": int(row.inflow or 0),
            "outflow": int(row.outflow or 0),
            "samples": int(row.samples or 0),
            "last_count": row.last_count,
        }
        for row in rows
    }

def get_inflow_outflow_bulk(
    db: Session, station_ids: list[int], hours: int = 1
) -> dict[int, dict]:
    """Same in/out delta logic as get_inflow_outflow(), computed for many
    stations in a single query instead of one round-trip per station -
    used by get_station_monitor() for the dashboard's Live Station
    Monitor widget so listing N stations doesn't cost N+1 queries.

    See crowd_flow_aggregate() above for how this stays bounded to one
    row per station regardless of the window's raw sample count."""
    result: dict[int, dict] = {sid: {"inflow": 0, "outflow": 0, "samples": 0} for sid in station_ids}
    if not station_ids:
        return result

    hours = _clamp_hours(hours)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    aggregate = crowd_flow_aggregate(db, station_ids, since)
    for station_id, values in aggregate.items():
        result[station_id] = {
            "inflow": values["inflow"],
            "outflow": values["outflow"],
            "samples": values["samples"],
        }

    return result

def get_station_monitor(db: Session, state: str | None = None, hours: int = 1) -> list[dict]:
    """Combined feed for the dashboard's Live Station Monitor widget:
    each active station's current density plus a short-window
    passenger in/out delta, busiest first. `state` filters to one
    city/state the same way every other crowd endpoint does."""
    snapshot = get_station_wise_snapshot(db, state)
    station_ids = [s["station_id"] for s in snapshot]
    flows = get_inflow_outflow_bulk(db, station_ids, hours=hours)

    for entry in snapshot:
        flow = flows.get(entry["station_id"], {"inflow": 0, "outflow": 0})
        entry["inflow"] = flow["inflow"]
        entry["outflow"] = flow["outflow"]

    snapshot.sort(key=lambda s: s["occupancy_ratio"] or 0, reverse=True)
    return snapshot



def get_inflow_outflow(db: Session, station_id: int, hours: int = 24) -> dict:
    """See crowd_flow_aggregate() above for how this stays bounded to a
    single aggregated row regardless of the window's raw sample count."""
    hours = _clamp_hours(hours)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    aggregate = crowd_flow_aggregate(db, [station_id], since)
    values = aggregate.get(station_id, {"inflow": 0, "outflow": 0, "samples": 0})

    return {
        "station_id": station_id,
        "window_hours": hours,
        "inflow": values["inflow"],
        "outflow": values["outflow"],
        "samples": values["samples"],
    }

def get_station_analytics(db: Session, station_id: int) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    stats = (
        db.query(
            func.avg(CrowdLog.current_count),
            func.max(CrowdLog.current_count),
            func.min(CrowdLog.current_count),
            func.count(CrowdLog.id),
        )
        .filter(CrowdLog.station_id == station_id, CrowdLog.created_at >= since)
        .first()
    )
    avg_count, max_count, min_count, sample_count = stats

    return {
        "station_id": station_id,
        "average_count_24h": round(avg_count, 1) if avg_count else 0,
        "peak_count_24h": max_count or 0,
        "min_count_24h": min_count or 0,
        "samples": sample_count or 0,
    }