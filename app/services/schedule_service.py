
import logging
from datetime import datetime, time
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
from app.utils.geo import cities_for_state
from app.websocket.events import DELAY_ALERT
from app.websocket.manager import manager

logger = logging.getLogger(__name__)

# Below this, a delay isn't worth interrupting every passenger's bell
# feed for - the live `delay_alert` socket event (used by the
# Dispatch Board / delay banners) still fires for every delay
# regardless, this threshold only gates the persisted Notification
# Center row.
DELAY_NOTIFICATION_THRESHOLD_MINUTES = 5

def _scope_to_state(query, state: str | None):
    """Joins in Station and filters to a state's cities, if requested."""
    cities = cities_for_state(state)
    if not cities:
        return query
    return query.join(Station, Station.id == TrainSchedule.station_id).filter(
        Station.city.in_(cities)
    )

_SCHEDULE_FIELDS = (
    "id", "train_id", "station_id", "arrival_time", "departure_time",
    "platform_number", "day_type", "is_peak_hour", "frequency_minutes",
    "status", "delay_minutes", "actual_arrival_time", "actual_departure_time",
)

def _serialize_schedule(s: TrainSchedule) -> dict:
    return {field: getattr(s, field) for field in _SCHEDULE_FIELDS}

def _hydrate_schedule(d: dict) -> TrainSchedule:
    """Rebuild a (detached, not session-bound) TrainSchedule from a
    cached dict. Only ever used for read responses - TrainScheduleResponse
    (app/schemas/train_schedule.py) only reads these same plain columns,
    never the `.train`/`.station` relationships, so a relationship-less
    object reconstructed straight from JSON is safe to serialize."""
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
) -> list[TrainSchedule]:
    """General schedule listing - the most-hit read on this router (every
    schedule-board load/refresh), but previously the only one of the
    three read paths here with zero caching (peak_hour_schedules/
    delayed_schedules already cached, this one still hit Postgres on
    every call). Cached the same way, keyed on the full filter set so
    different station/train/day/state combinations don't collide."""
    cache_key = f"schedule:list:{station_id}:{train_id}:{day_type.value if day_type else None}:{state}"

    def _compute() -> list[TrainSchedule]:
        query = db.query(TrainSchedule)
        if station_id:
            query = query.filter(TrainSchedule.station_id == station_id)
        if train_id:
            query = query.filter(TrainSchedule.train_id == train_id)
        if day_type:
            query = query.filter(TrainSchedule.day_type == day_type)
        query = _scope_to_state(query, state)
        return query.order_by(TrainSchedule.arrival_time).all()

    return _cached_schedule_list(cache_key, _compute)

def _current_day_type() -> DayType:
    """Saturday/Sunday -> WEEKEND, else WEEKDAY. Schedules only carry a
    time-of-day (no date), so "today" is resolved this way rather than
    against a specific calendar date - matches how list_schedules'
    day_type filter is meant to be used."""
    return DayType.WEEKEND if datetime.now().weekday() >= 5 else DayType.WEEKDAY

def _station_line_info(station: Station | None) -> tuple[str | None, str | None]:
    """(line_name, line_color) for a station, resolved the same way as
    app/services/station_service.py::_attach_line_info - Station has
    no line_name/line_color columns of its own, that info lives on
    MetroLine via the line_stations join table. Picks the first
    associated line; every station in the current dataset belongs to
    exactly one."""
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
    """Feed for the "Upcoming Train Schedule" widget: each row is one
    train's next stop from now, with the stop right after it (in that
    same train's timetable) as "To" - not a separate route table, this
    project doesn't have one, so consecutive same-train schedule rows
    in time order stand in for the route. `state` scopes to one
    city/state the same way every other schedule endpoint does.

    Defensive by design: this backs a dashboard widget, not a critical
    workflow, so any unexpected failure here is logged and swallowed
    (returns []) instead of bubbling into a 500 - a widget that
    silently shows "no data" is a much better failure mode than one
    that breaks the CORS response and shows as a confusing "can't
    reach the server" network error in the browser.
    """
    try:
        day_type = _current_day_type()
        now_t = datetime.now().time()

        base = (
            db.query(TrainSchedule)
            .join(Station, Station.id == TrainSchedule.station_id)
            .options(
                joinedload(TrainSchedule.station)
                .joinedload(Station.metro_lines)
                .joinedload(LineStation.line)
            )
            .filter(TrainSchedule.day_type == day_type)
        )
        cities = cities_for_state(state)
        if cities:
            base = base.filter(Station.city.in_(cities))
        if status:
            base = base.filter(TrainSchedule.status == status)

        upcoming = (
            base.filter(TrainSchedule.departure_time >= now_t)
            .order_by(TrainSchedule.departure_time.asc())
            .limit(limit)
            .all()
        )
        if not upcoming:
            # Nothing left for the rest of today under this filter -
            # fall back to the day's earliest matches so the widget
            # isn't empty right after the last train of the day departs.
            upcoming = base.order_by(TrainSchedule.departure_time.asc()).limit(limit).all()

        train_ids = {s.train_id for s in upcoming}
        if not train_ids:
            return []

        # Full same-day timetable for just these trains, to find each
        # picked row's next stop.
        timetable_rows = (
            db.query(TrainSchedule)
            .filter(TrainSchedule.train_id.in_(train_ids), TrainSchedule.day_type == day_type)
            .order_by(TrainSchedule.departure_time.asc())
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
            next_stop = (
                siblings[idx + 1] if idx is not None and idx + 1 < len(siblings) else None
            )
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
    """Drop every cached *unfiltered* view a write to `schedule` can affect.

    MILESTONE 9 FIX: this used to be duplicated ad-hoc (and incompletely)
    inside `handle_delay` alone - `adjust_frequency` called none of this
    at all, so an operator hitting "Adjust frequency" changed the row in
    Postgres but every dashboard kept serving the pre-edit
    frequency_minutes/is_peak_hour for up to SCHEDULE_CACHE_TTL_SECONDS,
    which is what showed up as stale/incorrect values ("--" once a
    filtered view happened to go from non-empty to empty, or just the
    old number otherwise) on the Train Scheduling page. Reproduced and
    confirmed against a real (in-memory) Redis-backed cache before this
    fix: `list_schedules(state=None)` kept returning the pre-write
    frequency_minutes/delay_minutes after adjust_frequency()/handle_delay()
    committed the new value, because their cache key
    (`schedule:list:None:None:None:None` - the default, no-city-selected
    Dispatch Board query) was never among the keys either function
    invalidated.

    Covers the *unfiltered* (state=None) slice of every cache key shape
    this schedule can appear under - the plain "list everything" key
    every load of the Dispatch Board with no city selected hits, plus
    the train/station-scoped list keys and the peak/delayed snapshot
    keys. Per-state-filtered keys are intentionally left to expire via
    TTL alone, same documented tradeoff as before (small enough key
    space, short enough TTL, not worth tracking every state a station's
    city could be filtered under)."""
    cache.delete("schedule:list:None:None:None:None")
    cache.delete(f"schedule:list:None:{schedule.train_id}:None:None")
    cache.delete(f"schedule:list:{schedule.station_id}:None:None:None")
    cache.delete("schedule:peak:None:None")
    cache.delete(f"schedule:peak:{schedule.station_id}:None")
    cache.delete("schedule:delayed:None:None")
    cache.delete(f"schedule:delayed:{schedule.station_id}:None")

def create_schedule(db: Session, payload: TrainScheduleCreate) -> TrainSchedule:
    data = payload.model_dump()
                                                            
    if not data.get("is_peak_hour"):
        data["is_peak_hour"] = _is_peak(data["arrival_time"])

    schedule = TrainSchedule(**data)
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule

def update_schedule(db: Session, schedule_id: int, payload: TrainScheduleUpdate) -> TrainSchedule:
    schedule = get_schedule(db, schedule_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(schedule, field, value)
    db.commit()
    db.refresh(schedule)
    return schedule

def handle_delay(db: Session, schedule_id: int, payload: DelayUpdate) -> TrainSchedule:
    """Delay handling workflow: records delay minutes and flips status."""
    schedule = get_schedule(db, schedule_id)
    schedule.delay_minutes = payload.delay_minutes
    schedule.status = ScheduleStatus.DELAYED if payload.delay_minutes > 0 else ScheduleStatus.ON_TIME

    if payload.delay_minutes > 0:
        base = datetime.combine(datetime.today(), schedule.arrival_time)
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
    """Frequency adjustment workflow (manual override of AI recommendation).

    MILESTONE 9 FIX: this previously had NO cache invalidation at all -
    every list_schedules()/peak_hour_schedules() cache entry already
    warmed kept serving the pre-edit frequency_minutes/is_peak_hour for
    up to SCHEDULE_CACHE_TTL_SECONDS after a commit here. Reproduced
    against a real (in-memory) Redis-backed cache: adjusting a
    schedule's frequency from 10 -> 3 and flipping is_peak_hour to True
    left `list_schedules(state=None)` (the Dispatch Board's default,
    no-city-selected query) still returning 10/False. See
    `_invalidate_schedule_caches` for the shared fix."""
    schedule = get_schedule(db, schedule_id)
    schedule.frequency_minutes = payload.frequency_minutes
    if payload.is_peak_hour is not None:
        schedule.is_peak_hour = payload.is_peak_hour
    db.commit()
    db.refresh(schedule)

    _invalidate_schedule_caches(schedule)

    return schedule

def peak_hour_schedules(
    db: Session,
    station_id: int | None = None,
    state: str | None = None,
) -> list[TrainSchedule]:
    """Peak-hour optimization view: schedules currently flagged as peak.
    Cached (short TTL) - this is a frequently-refreshed dashboard view,
    same reasoning as the crowd dashboard snapshot."""
    cache_key = f"schedule:peak:{station_id}:{state}"

    def _compute() -> list[TrainSchedule]:
        query = db.query(TrainSchedule).filter(TrainSchedule.is_peak_hour.is_(True))
        if station_id:
            query = query.filter(TrainSchedule.station_id == station_id)
        query = _scope_to_state(query, state)
        return query.order_by(TrainSchedule.arrival_time).all()

    return _cached_schedule_list(cache_key, _compute)

def delayed_schedules(
    db: Session,
    station_id: int | None = None,
    state: str | None = None,
) -> list[TrainSchedule]:
    """Delay handling: currently delayed schedule entries - feeds the
    dashboard's "Average Delay" KPI, so this is hit on essentially every
    dashboard load/refresh. Cached (short TTL) for the same reason as
    peak_hour_schedules above.

    BUGFIX: this used to filter on `status == DELAYED` only. `status`
    is a separate field from `delay_minutes` and is only flipped to
    DELAYED by the manual operator `handle_delay` workflow - it is
    NOT derived automatically from delay_minutes anywhere else in
    the app (e.g. seeded/synced rows can have a real delay_minutes
    value with status still ON_TIME). That made this endpoint return
    0 rows forever even with real delay data sitting in the DB.
    Filter on the actual delay value instead (same pattern already
    used in analytics_service.py), and keep the OR on status so any
    row a human explicitly flagged DELAYED still shows even if
    delay_minutes hasn't been (re)recorded."""
    cache_key = f"schedule:delayed:{station_id}:{state}"

    def _compute() -> list[TrainSchedule]:
        query = db.query(TrainSchedule).filter(
            (TrainSchedule.delay_minutes > 0) | (TrainSchedule.status == ScheduleStatus.DELAYED)
        )
        if station_id:
            query = query.filter(TrainSchedule.station_id == station_id)
        query = _scope_to_state(query, state)
        return query.order_by(TrainSchedule.delay_minutes.desc()).all()

    return _cached_schedule_list(cache_key, _compute)
