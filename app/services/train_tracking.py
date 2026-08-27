import time as _time
from datetime import time

from sqlalchemy.orm import Session

from app.core import cache
from app.models.train_schedule import TrainSchedule

MIN_SEGMENT_SECONDS = 30

_ROUTE_CACHE_TTL_SECONDS = 300
_ROUTE_CACHE_KEY = "schedule:routes"
_routes_cache: dict[int, list[int]] = {}
_segment_seconds_cache: dict[int, list[int]] = {}
_routes_cache_at: float = 0.0

def seconds_since_midnight(t: time) -> int:
    return t.hour * 3600 + t.minute * 60 + t.second

def segment_gap_seconds(start: time, end: time) -> int:
    diff = seconds_since_midnight(end) - seconds_since_midnight(start)
    if diff <= 0:
        diff += 24 * 3600
    return max(diff, MIN_SEGMENT_SECONDS)

def _load_from_redis() -> bool:
    """Try to hydrate the process-local cache from Redis instead of
    hitting Postgres. Returns True on a usable hit. JSON round-trips
    dict keys as strings, so train_id is cast back to int on the way
    in."""
    global _routes_cache, _segment_seconds_cache, _routes_cache_at

    cached = cache.get_json(_ROUTE_CACHE_KEY)
    if not cached:
        return False

    _routes_cache = {int(tid): route for tid, route in cached.get("routes", {}).items()}
    _segment_seconds_cache = {
        int(tid): segs for tid, segs in cached.get("segment_seconds", {}).items()
    }
    _routes_cache_at = _time.monotonic()
    return True

def _refresh_route_cache(db: Session) -> None:
    """(Re)builds the route + segment-duration cache for every train
    that has a schedule. Deliberately not scoped to a specific
    train_ids list - a full refresh covers every train at once, so a
    request for a different subset of trains later doesn't trigger yet
    another rebuild."""
    global _routes_cache, _segment_seconds_cache, _routes_cache_at

    routes: dict[int, list[int]] = {}
    arrival_times: dict[int, list[time]] = {}
                                                               
    seen_stations: dict[int, set[int]] = {}
    # Only the 3 columns actually used below - pulling full ORM rows
    # (15 columns incl. platform_number, day_type, actual_*, timestamps
    # etc., none read here) multiplies the payload for no reason. On a
    # schedule table with hundreds of thousands of rows that's enough
    # extra transfer time/size for Postgres to drop the connection
    # mid-query, which leaves routes empty (and Active Trains stuck at
    # 0) run after run even though nothing actually errors loudly.
    schedules = (
        db.query(TrainSchedule.train_id, TrainSchedule.station_id, TrainSchedule.arrival_time)
        .order_by(TrainSchedule.train_id, TrainSchedule.arrival_time, TrainSchedule.id)
        .all()
    )

    for s in schedules:
        route = routes.setdefault(s.train_id, [])
        arr_list = arrival_times.setdefault(s.train_id, [])
        seen = seen_stations.setdefault(s.train_id, set())
        if s.station_id in seen:
            continue
        seen.add(s.station_id)
        route.append(s.station_id)
        arr_list.append(s.arrival_time)

    segment_seconds = {
        tid: [
            segment_gap_seconds(arr_list[i], arr_list[i + 1])
            for i in range(len(arr_list) - 1)
        ]
        for tid, arr_list in arrival_times.items()
    }

    _routes_cache = routes
    _segment_seconds_cache = segment_seconds
    _routes_cache_at = _time.monotonic()

    cache.set_json(
        _ROUTE_CACHE_KEY,
        {"routes": _routes_cache, "segment_seconds": _segment_seconds_cache},
        ttl_seconds=_ROUTE_CACHE_TTL_SECONDS,
    )

def _delay_by_station_for(db: Session, train_ids: list[int]) -> dict[int, dict[int, int]]:
    """Always fetched fresh (never cached) - this is exactly the part
    of build_routes() that must never be stale, since delay_minutes is
    what live tracking exists to surface."""
    if not train_ids:
        return {}
    rows = (
        db.query(TrainSchedule.train_id, TrainSchedule.station_id, TrainSchedule.delay_minutes)
        .filter(TrainSchedule.train_id.in_(train_ids))
        .all()
    )
    delay_by_station: dict[int, dict[int, int]] = {}
    for train_id, station_id, delay_minutes in rows:
        delay_by_station.setdefault(train_id, {})[station_id] = delay_minutes or 0
    return delay_by_station

def build_routes(
    db: Session, train_ids: list[int]
) -> tuple[dict[int, list[int]], dict[int, list[int]], dict[int, dict[int, int]]]:
    if not train_ids:
        return {}, {}, {}

    stale = (_time.monotonic() - _routes_cache_at) >= _ROUTE_CACHE_TTL_SECONDS
    missing = any(tid not in _routes_cache for tid in train_ids)
    if stale or missing:
                                                                     
        if not _load_from_redis() or any(tid not in _routes_cache for tid in train_ids):
            _refresh_route_cache(db)

    routes = {tid: _routes_cache.get(tid, []) for tid in train_ids}
    segment_seconds = {tid: _segment_seconds_cache.get(tid, []) for tid in train_ids}
    delay_by_station = _delay_by_station_for(db, train_ids)

    return routes, segment_seconds, delay_by_station

def segment_index_for(index: int, direction: int, segment_count: int) -> int:
    if segment_count <= 0:
        return 0
    raw = index if direction == 1 else index - 1
    return max(0, min(raw, segment_count - 1))

def speed_factor_for(delay_minutes: int) -> float:
    return 1.0 / (1.0 + delay_minutes / 10.0)

def eta_seconds_for(progress_ratio: float, segment_duration: int, speed_factor: float) -> int:
    remaining = max(0.0, 1.0 - progress_ratio)
    effective_duration = segment_duration / speed_factor if speed_factor else segment_duration
    return round(remaining * effective_duration)

def walk_eta_seconds(
    route: list[int],
    segment_seconds: list[int],
    from_index: int,
    direction: int,
    remaining_current_segment: float,
    speed_factor: float,
    target_index: int,
) -> int | None:
    if not route or not segment_seconds or target_index < 0 or target_index >= len(route):
        return None

    pos = from_index + direction
    pos = max(0, min(len(route) - 1, pos))
    dir_ = direction
    total = remaining_current_segment

    if pos == target_index:
        return round(total)

    for _ in range(2 * len(route)):
        if pos == len(route) - 1:
            dir_ = -1
        elif pos == 0:
            dir_ = 1

        seg_index = pos if dir_ == 1 else pos - 1
        seg_index = max(0, min(len(segment_seconds) - 1, seg_index))
        segment_duration = segment_seconds[seg_index]
        total += segment_duration / speed_factor if speed_factor else segment_duration

        pos += dir_
        pos = max(0, min(len(route) - 1, pos))

        if pos == target_index:
            return round(total)

    return None