
import logging
from datetime import datetime, time, timezone
from typing import Callable

from fastapi import HTTPException
from sqlalchemy.orm import Session, joinedload

from app.core import cache
from app.core.config import settings
from app.enums.day_type import DayType
from app.enums.notification_source import NotificationSource
from app.enums.schedule_status import ScheduleStatus
from app.models.line_station import LineStation
from app.models.station import Station
from app.models.train import Train
from app.models.train_schedule import TrainSchedule
from app.schemas.train_schedule import (
    DelayUpdate,
    FrequencyAdjustment,
    TrainScheduleCreate,
    TrainScheduleUpdate,
)
from app.services import notification_service
from app.services.train_tracking import invalidate_route_cache
from app.utils.geo import cities_for_state
from app.utils.timezone import business_now, business_today
from app.websocket.events import DELAY_ALERT, SCHEDULE_UPDATE
from app.websocket.manager import manager

logger = logging.getLogger(__name__)

DELAY_NOTIFICATION_THRESHOLD_MINUTES = 5

DEFAULT_SCHEDULE_LIST_LIMIT = 200
MAX_SCHEDULE_LIST_LIMIT = 1000

def _clamp(value: int | None, default: int, maximum: int) -> int:
    if value is None:
        value = default
    return min(max(value, 1), maximum)

def _scope_to_state(query, state: str | None):
    cities = cities_for_state(state)
    if not cities:
        return query
    return query.join(Station, Station.id == TrainSchedule.station_id).filter(
        Station.city.in_(cities)
    )

_SCHEDULE_FIELDS = (
    "id", "train_id", "station_id", "arrival_time", "departure_time",
    "platform_number", "station_sequence", "day_type", "is_peak_hour",
    "frequency_minutes", "status", "delay_minutes", "actual_arrival_time",
    "actual_departure_time",
)

def _serialize_schedule(s: TrainSchedule) -> dict:
    return {field: getattr(s, field) for field in _SCHEDULE_FIELDS}

def _hydrate_schedule(d: dict) -> TrainSchedule:
    data = dict(d)
    for key in ("arrival_time", "departure_time", "actual_arrival_time", "actual_departure_time"):
        if data.get(key):
            data[key] = time.fromisoformat(data[key])
    return TrainSchedule(**data)

def _cached_schedule_list(cache_key: str, compute: "Callable[[], list[TrainSchedule]]") -> list[TrainSchedule]:
    cached = cache.get_json(cache_key)
    if cached is not None:
        return [_hydrate_schedule(row) for row in cached]

    rows = compute()
    cache.set_json(
        cache_key,
        [_serialize_schedule(s) for s in rows],
        ttl_seconds=settings.SCHEDULE_CACHE_TTL_SECONDS,
    )
    return rows

PEAK_WINDOWS = [
    (time(8, 0), time(11, 0)),
    (time(17, 0), time(20, 0)),
]

def _is_peak(t: time) -> bool:
    return any(start <= t <= end for start, end in PEAK_WINDOWS)

def list_schedules(
    db: Session,
    station_id: int | None = None,
    train_id: int | None = None,
    day_type: DayType | None = None,
    state: str | None = None,
    limit: int = DEFAULT_SCHEDULE_LIST_LIMIT,
    offset: int = 0,
) -> list[TrainSchedule]:
    limit = _clamp(limit, DEFAULT_SCHEDULE_LIST_LIMIT, MAX_SCHEDULE_LIST_LIMIT)
    offset = max(offset or 0, 0)
    cache_key = (
        f"schedule:list:{station_id}:{train_id}:"
        f"{day_type.value if day_type else None}:{state}:{limit}:{offset}"
    )

    def _compute() -> list[TrainSchedule]:
        query = db.query(TrainSchedule)
        if station_id:
            query = query.filter(TrainSchedule.station_id == station_id)
        if train_id:
            query = query.filter(TrainSchedule.train_id == train_id)
        if day_type:
            query = query.filter(TrainSchedule.day_type == day_type)
        query = _scope_to_state(query, state)
        return query.order_by(TrainSchedule.arrival_time).offset(offset).limit(limit).all()

    return _cached_schedule_list(cache_key, _compute)

def _current_day_type() -> DayType:
    return DayType.WEEKEND if business_now().weekday() >= 5 else DayType.WEEKDAY

def _station_line_info(station: Station | None) -> tuple[str | None, str | None]:
    if station is None:
        return None, None
    link = station.metro_lines[0] if station.metro_lines else None
    if not link or not link.line:
        return None, None
    return link.line.line_name, link.line.color

def get_upcoming_schedules(
    db: Session,
    state: str | None = None,
    status: ScheduleStatus | None = None,
    limit: int = 20,
) -> list[dict]:
    try:
        limit = _clamp(limit, 20, MAX_SCHEDULE_LIST_LIMIT)
        day_type = _current_day_type()
        now_t = business_now().time()

        base = db.query(TrainSchedule).options(
            joinedload(TrainSchedule.station)
            .joinedload(Station.metro_lines)
            .joinedload(LineStation.line)
        ).filter(TrainSchedule.day_type == day_type)

        cities = cities_for_state(state)
        if cities:
            base = base.join(Station, Station.id == TrainSchedule.station_id).filter(
                Station.city.in_(cities)
            )
        if status:
            base = base.filter(TrainSchedule.status == status)

        def _dedupe_by_train(rows: list[TrainSchedule], cap: int) -> list[TrainSchedule]:
            seen: set[int] = set()
            deduped: list[TrainSchedule] = []
            for row in rows:
                if row.train_id in seen:
                    continue
                seen.add(row.train_id)
                deduped.append(row)
                if len(deduped) >= cap:
                    break
            return deduped

        candidates = (
            base.filter(TrainSchedule.departure_time >= now_t)
            .order_by(TrainSchedule.departure_time.asc())
            .limit(limit * 50)
            .all()
        )
        upcoming = _dedupe_by_train(candidates, limit)
        if not upcoming:

            candidates = base.order_by(TrainSchedule.departure_time.asc()).limit(limit * 50).all()
            upcoming = _dedupe_by_train(candidates, limit)

        train_ids = {s.train_id for s in upcoming}
        if not train_ids:
            return []

        timetable_rows = (
            db.query(TrainSchedule)
            .options(joinedload(TrainSchedule.station))
            .filter(TrainSchedule.train_id.in_(train_ids), TrainSchedule.day_type == day_type)
            .order_by(TrainSchedule.station_sequence.asc().nulls_last())
            .all()
        )
        by_train: dict[int, list[TrainSchedule]] = {}
        for row in timetable_rows:
            by_train.setdefault(row.train_id, []).append(row)

        trains = {t.id: t for t in db.query(Train).filter(Train.id.in_(train_ids)).all()}

        results = []
        for s in upcoming:
            siblings = by_train.get(s.train_id, [])
            idx = next((i for i, r in enumerate(siblings) if r.id == s.id), None)
            next_stop = None
            if idx is not None and s.station_sequence is not None:

                for candidate in siblings[idx + 1:]:
                    if candidate.station_sequence is not None and candidate.station_sequence > s.station_sequence:
                        next_stop = candidate
                        break
            train = trains.get(s.train_id)
            station = getattr(s, "station", None)
            next_station = getattr(next_stop, "station", None) if next_stop else None
            line_name, line_color = _station_line_info(station)
            results.append(
                {
                    "id": s.id,
                    "train_id": s.train_id,
                    "train_number": train.train_number if train else f"#{s.train_id}",
                    "from_station_id": s.station_id,
                    "from_station_name": station.station_name if station else "Unknown",
                    "line_name": line_name,
                    "line_color": line_color,
                    "to_station_name": next_station.station_name if next_station else None,
                    "departure_time": s.departure_time.isoformat(),
                    "status": s.status,
                    "delay_minutes": s.delay_minutes,
                }
            )
        return results
    except Exception:
        logger.exception(
            "get_upcoming_schedules failed (state=%r, status=%r) - returning empty list",
            state,
            status,
        )
        return []

def get_schedule(db: Session, schedule_id: int) -> TrainSchedule:
    schedule = db.get(TrainSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return schedule

def _invalidate_schedule_caches(schedule: TrainSchedule) -> None:
    default_page = f"{DEFAULT_SCHEDULE_LIST_LIMIT}:0"
    cache.delete(f"schedule:list:None:None:None:None:{default_page}")
    cache.delete(f"schedule:list:None:{schedule.train_id}:None:None:{default_page}")
    cache.delete(f"schedule:list:{schedule.station_id}:None:None:None:{default_page}")
    cache.delete(f"schedule:peak:None:None:{default_page}")
    cache.delete(f"schedule:peak:{schedule.station_id}:None:{default_page}")
    cache.delete(f"schedule:delayed:None:None:{default_page}")
    cache.delete(f"schedule:delayed:{schedule.station_id}:None:{default_page}")

def _broadcast_schedule_update(db: Session, schedule: TrainSchedule) -> None:
    train = db.get(Train, schedule.train_id)
    station = db.get(Station, schedule.station_id)
    manager.notify(SCHEDULE_UPDATE, {
        "schedule_id": schedule.id,
        "train_id": schedule.train_id,
        "train_number": train.train_number if train else None,
        "station_id": schedule.station_id,
        "station_name": station.station_name if station else None,
        "arrival_time": schedule.arrival_time.isoformat() if schedule.arrival_time else None,
        "departure_time": schedule.departure_time.isoformat() if schedule.departure_time else None,
        "platform_number": schedule.platform_number,
        "status": schedule.status.value if schedule.status else None,
        "delay_minutes": schedule.delay_minutes,
        "frequency_minutes": schedule.frequency_minutes,
        "is_peak_hour": schedule.is_peak_hour,
    })

def create_schedule(db: Session, payload: TrainScheduleCreate) -> TrainSchedule:
    data = payload.model_dump()
                                                            
    if not data.get("is_peak_hour"):
        data["is_peak_hour"] = _is_peak(data["arrival_time"])

    schedule = TrainSchedule(**data)
    db.add(schedule)
    db.commit()
    db.refresh(schedule)

    _invalidate_schedule_caches(schedule)
    invalidate_route_cache()
    _broadcast_schedule_update(db, schedule)
    return schedule

def update_schedule(db: Session, schedule_id: int, payload: TrainScheduleUpdate) -> TrainSchedule:
    schedule = get_schedule(db, schedule_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(schedule, field, value)
    db.commit()
    db.refresh(schedule)

    _invalidate_schedule_caches(schedule)
    invalidate_route_cache()
    _broadcast_schedule_update(db, schedule)
    return schedule

def handle_delay(db: Session, schedule_id: int, payload: DelayUpdate) -> TrainSchedule:
    schedule = get_schedule(db, schedule_id)
    schedule.delay_minutes = payload.delay_minutes
    schedule.status = ScheduleStatus.DELAYED if payload.delay_minutes > 0 else ScheduleStatus.ON_TIME

    if payload.delay_minutes > 0:
        base = datetime.combine(business_today(), schedule.arrival_time)
        delayed = base.replace(
            minute=(base.minute + payload.delay_minutes) % 60,
            hour=base.hour + (base.minute + payload.delay_minutes) // 60,
        )
        schedule.actual_arrival_time = delayed.time()

    db.commit()
    db.refresh(schedule)

    _invalidate_schedule_caches(schedule)

    train = db.get(Train, schedule.train_id)
    station = db.get(Station, schedule.station_id)
    manager.notify(DELAY_ALERT, {
        "schedule_id": schedule.id,
        "train_id": schedule.train_id,
        "train_number": train.train_number if train else None,
        "station_id": schedule.station_id,
        "station_name": station.station_name if station else None,
        "delay_minutes": schedule.delay_minutes,
        "status": schedule.status.value,
    })

    if schedule.delay_minutes >= DELAY_NOTIFICATION_THRESHOLD_MINUTES:
        train_label = train.train_number if train else f"Train #{schedule.train_id}"
        station_label = station.station_name if station else f"Station #{schedule.station_id}"
        notification_service.create_notification(
            db,
            source=NotificationSource.SYSTEM,
            title=f"Delay - {train_label}",
            message=f"{train_label} is running {schedule.delay_minutes} min late at {station_label}.",
            state=station.city if station else None,
        )

    return schedule

def adjust_frequency(db: Session, schedule_id: int, payload: FrequencyAdjustment) -> TrainSchedule:
    schedule = get_schedule(db, schedule_id)
    schedule.frequency_minutes = payload.frequency_minutes
    if payload.is_peak_hour is not None:
        schedule.is_peak_hour = payload.is_peak_hour
    db.commit()
    db.refresh(schedule)

    _invalidate_schedule_caches(schedule)

    return schedule

def build_peak_frequency_optimization_preview(
    db: Session,
    state: str | None = None,
    limit: int = 50,
) -> dict:
    from app.services import prediction_service
    from app.models.station_crowd_state import StationCrowdState

    safe_limit = min(max(limit, 1), 100)
    schedules = peak_hour_schedules(db, state=state, limit=safe_limit)
    station_ids = {schedule.station_id for schedule in schedules}
    stations = {
        station.id: station
        for station in db.query(Station).filter(Station.id.in_(station_ids)).all()
    } if station_ids else {}
    crowd_states = {
        row.station_id: row
        for row in db.query(StationCrowdState).filter(StationCrowdState.station_id.in_(station_ids)).all()
    } if station_ids else {}
    recommendations = {}
    items = []

    for schedule in schedules:
        station = stations.get(schedule.station_id)
        if station is None:
            continue
        if station.id not in recommendations:
            recommendations[station.id] = prediction_service.recommend_train_frequency(
                db, station.id, is_peak_hour=True
            )
        prediction = recommendations[station.id]
        recommended = min(30, max(1, round(prediction.predicted_value)))
        crowd = crowd_states.get(station.id)
        items.append({
            "schedule_id": schedule.id,
            "station_id": station.id,
            "station_name": station.station_name,
            "train_id": schedule.train_id,
            "current_frequency_minutes": schedule.frequency_minutes,
            "recommended_frequency_minutes": recommended,
            "current_count": crowd.current_count if crowd else None,
            "capacity": station.capacity,
            "current_occupancy_pct": round(crowd.current_count / station.capacity * 100, 1)
            if crowd and station.capacity else None,
            "model_version": prediction.model_version,
            "changed": recommended != schedule.frequency_minutes,
        })

    metrics = prediction_service.get_frequency_model_metrics()
    return {
        "state": state,
        "generated_at": datetime.now(timezone.utc),
        "applied": False,
        "model_evaluation": {
            "available": metrics.get("available", False),
            "model_name": metrics.get("model_name"),
            "mae": metrics.get("mae"),
            "test_rows": metrics.get("test_rows"),
            "source": "held-out training dataset",
        },
        "operational_impact": "pending real post-adjustment observations",
        "items": items,
    }

def peak_hour_schedules(
    db: Session,
    station_id: int | None = None,
    state: str | None = None,
    limit: int = DEFAULT_SCHEDULE_LIST_LIMIT,
    offset: int = 0,
) -> list[TrainSchedule]:
    limit = _clamp(limit, DEFAULT_SCHEDULE_LIST_LIMIT, MAX_SCHEDULE_LIST_LIMIT)
    offset = max(offset or 0, 0)
    cache_key = f"schedule:peak:{station_id}:{state}:{limit}:{offset}"

    def _compute() -> list[TrainSchedule]:
        query = db.query(TrainSchedule).filter(TrainSchedule.is_peak_hour.is_(True))
        if station_id:
            query = query.filter(TrainSchedule.station_id == station_id)
        query = _scope_to_state(query, state)
        return query.order_by(TrainSchedule.arrival_time).offset(offset).limit(limit).all()

    return _cached_schedule_list(cache_key, _compute)

def delayed_schedules(
    db: Session,
    station_id: int | None = None,
    state: str | None = None,
    limit: int = DEFAULT_SCHEDULE_LIST_LIMIT,
    offset: int = 0,
) -> list[TrainSchedule]:
    limit = _clamp(limit, DEFAULT_SCHEDULE_LIST_LIMIT, MAX_SCHEDULE_LIST_LIMIT)
    offset = max(offset or 0, 0)
    cache_key = f"schedule:delayed:{station_id}:{state}:{limit}:{offset}"

    def _compute() -> list[TrainSchedule]:
        query = db.query(TrainSchedule).filter(
            (TrainSchedule.delay_minutes > 0) | (TrainSchedule.status == ScheduleStatus.DELAYED)
        )
        if station_id:
            query = query.filter(TrainSchedule.station_id == station_id)
        query = _scope_to_state(query, state)
        return query.order_by(TrainSchedule.delay_minutes.desc()).offset(offset).limit(limit).all()

    return _cached_schedule_list(cache_key, _compute)

def delayed_schedules_count(
    db: Session,
    station_id: int | None = None,
    state: str | None = None,
) -> int:
    cache_key = f"schedule:delayed:count:{station_id}:{state}"
    cached = cache.get_json(cache_key)
    if cached is not None:
        return cached

    query = db.query(TrainSchedule).filter(
        (TrainSchedule.delay_minutes > 0) | (TrainSchedule.status == ScheduleStatus.DELAYED)
    )
    if station_id:
        query = query.filter(TrainSchedule.station_id == station_id)
    query = _scope_to_state(query, state)
    count = query.count()

    cache.set_json(cache_key, count, ttl_seconds=settings.SCHEDULE_CACHE_TTL_SECONDS)
    return count
