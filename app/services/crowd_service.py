from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import cache
from app.enums.crowd_level import CrowdLevel
from app.models.crowd_log import CrowdLog
from app.models.station import Station
from app.schemas.crowd_log import CrowdLogCreate
from app.utils.geo import cities_for_state, state_for_city

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
    db.commit()
    db.refresh(log)
    invalidate_station_cache(station)
    return log

def get_latest_crowd(db: Session, station_id: int) -> CrowdLog | None:
                                                                         
    cache_key = f"crowd:latest:{station_id}"
    cached = cache.get_json(cache_key)
    if cached is not None:
        return CrowdLog(**cached)

    log = (
        db.query(CrowdLog)
        .filter(CrowdLog.station_id == station_id)
        .order_by(CrowdLog.created_at.desc())
        .first()
    )
    if log is not None:
        cache.set_json(cache_key, {
            "id": log.id,
            "station_id": log.station_id,
            "current_count": log.current_count,
            "crowd_level": log.crowd_level,
            "created_at": log.created_at,
        })
    return log

def get_station_wise_snapshot(db: Session, state: str | None = None) -> list[dict]:
                                                                          
    cache_key = f"crowd:dashboard:{state or 'all'}"
    cached = cache.get_json(cache_key)
    if cached is not None:
        return cached

    snapshot = _get_station_wise_snapshot_from_db(db, state)
    cache.set_json(cache_key, snapshot)
    return snapshot

def _get_station_wise_snapshot_from_db(db: Session, state: str | None = None) -> list[dict]:
    latest_per_station = (
        db.query(
            CrowdLog.station_id.label("station_id"),
            CrowdLog.current_count.label("current_count"),
            CrowdLog.crowd_level.label("crowd_level"),
            CrowdLog.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=CrowdLog.station_id,
                order_by=CrowdLog.created_at.desc(),
            )
            .label("rn"),
        )
        .subquery()
    )

    query = (
        db.query(
            Station,
            latest_per_station.c.current_count,
            latest_per_station.c.crowd_level,
            latest_per_station.c.created_at,
        )
        .outerjoin(
            latest_per_station,
            (latest_per_station.c.station_id == Station.id)
            & (latest_per_station.c.rn == 1),
        )
        .filter(Station.is_active.is_(True))
    )

    cities = cities_for_state(state)
    if cities:
        query = query.filter(Station.city.in_(cities))

    rows = query.all()

    snapshot = []
    for station, current_count, crowd_level, last_updated in rows:
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
        })
    return snapshot

def get_heatmap(db: Session, state: str | None = None, limit: int | None = None) -> list[dict]:
    snapshot = get_station_wise_snapshot(db, state)
    heatmap = [
        entry for entry in snapshot
        if entry["latitude"] is not None
        and entry["longitude"] is not None
        and not (entry["latitude"] == 0 and entry["longitude"] == 0)
    ]
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


def get_inflow_outflow_bulk(
    db: Session, station_ids: list[int], hours: int = 1
) -> dict[int, dict]:
    """Same in/out delta logic as get_inflow_outflow(), computed for many
    stations in a single query instead of one round-trip per station -
    used by get_station_monitor() for the dashboard's Live Station
    Monitor widget so listing N stations doesn't cost N+1 queries."""
    result: dict[int, dict] = {sid: {"inflow": 0, "outflow": 0, "samples": 0} for sid in station_ids}
    if not station_ids:
        return result

    since = datetime.utcnow() - timedelta(hours=hours)
    logs = (
        db.query(CrowdLog)
        .filter(CrowdLog.station_id.in_(station_ids), CrowdLog.created_at >= since)
        .order_by(CrowdLog.station_id.asc(), CrowdLog.created_at.asc())
        .all()
    )

    previous_by_station: dict[int, int] = {}
    for log in logs:
        entry = result[log.station_id]
        entry["samples"] += 1
        previous_count = previous_by_station.get(log.station_id)
        if previous_count is not None:
            delta = log.current_count - previous_count
            if delta > 0:
                entry["inflow"] += delta
            else:
                entry["outflow"] += abs(delta)
        previous_by_station[log.station_id] = log.current_count

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
    since = datetime.utcnow() - timedelta(hours=hours)
    logs = (
        db.query(CrowdLog)
        .filter(CrowdLog.station_id == station_id, CrowdLog.created_at >= since)
        .order_by(CrowdLog.created_at.asc())
        .all()
    )

    inflow = 0
    outflow = 0
    previous_count = None
    for log in logs:
        if previous_count is not None:
            delta = log.current_count - previous_count
            if delta > 0:
                inflow += delta
            else:
                outflow += abs(delta)
        previous_count = log.current_count

    return {
        "station_id": station_id,
        "window_hours": hours,
        "inflow": inflow,
        "outflow": outflow,
        "samples": len(logs),
    }

def get_station_analytics(db: Session, station_id: int) -> dict:
    since = datetime.utcnow() - timedelta(hours=24)
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
