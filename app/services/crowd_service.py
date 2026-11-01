from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased

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

MAX_HISTORY_WINDOW_HOURS = 720  

def _clamp_hours(hours: int) -> int:
    return min(max(hours or 1, 1), MAX_HISTORY_WINDOW_HOURS)

def invalidate_station_cache(station: Station) -> None:
    cache.delete(f"crowd:latest:{station.id}")
    cache.delete("crowd:dashboard:all")
    state = state_for_city(station.city)
    if state:
        cache.delete(f"crowd:dashboard:{state}")
                                                                        
    cache.delete(f"crowd:dashboard:{station.city}")

def log_crowd_count(db: Session, payload: CrowdLogCreate) -> CrowdLog:
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

def upsert_live_state(
    db: Session,
    station_id: int,
    current_count: int,
    level: CrowdLevel,
    updated_at: datetime | None = None,
) -> None:
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
            "updated_at": updated_at or func.now(),
        },
    )
    db.execute(stmt)

def apply_live_state_delta(db: Session, station: Station, delta: int) -> tuple[int, CrowdLevel]:
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

        and (entry.get("capacity") or 0) > 0
    ]

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
    if not station_ids:
        return {}

    previous_log = aliased(CrowdLog)
    baseline_at = (
        select(func.max(previous_log.created_at))
        .where(
            previous_log.station_id == CrowdLog.station_id,
            previous_log.created_at < since,
        )
        .correlate(CrowdLog)
        .scalar_subquery()
    )
    ordered = (
        db.query(
            CrowdLog.station_id.label("station_id"),
            CrowdLog.current_count.label("current_count"),
            CrowdLog.created_at.label("created_at"),
            func.lag(CrowdLog.current_count)
            .over(partition_by=CrowdLog.station_id, order_by=CrowdLog.created_at.asc())
            .label("prev_count"),
            func.row_number()
            .over(partition_by=CrowdLog.station_id, order_by=CrowdLog.created_at.desc())
            .label("rn_desc"),
        )
        .filter(
            CrowdLog.station_id.in_(station_ids),
            or_(CrowdLog.created_at >= since, CrowdLog.created_at == baseline_at),
        )
        .subquery()
    )

    delta = ordered.c.current_count - ordered.c.prev_count
    rows = (
        db.query(
            ordered.c.station_id,
            func.sum(case((ordered.c.created_at >= since, 1), else_=0)).label("samples"),
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

def get_station_analytics(db: Session, station_id: int, hours: int = 24) -> dict:
    hours = _clamp_hours(hours)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
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
        "window_hours": hours,
        "average_count_24h": round(avg_count, 1) if avg_count else 0,
        "peak_count_24h": max_count or 0,
        "min_count_24h": min_count or 0,
        "samples": sample_count or 0,
    }
