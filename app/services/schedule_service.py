
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

# Below this, a delay isn't worth interrupting every passenger's bell
# feed for - the live `delay_alert` socket event (used by the
# Dispatch Board / delay banners) still fires for every delay
# regardless, this threshold only gates the persisted Notification
# Center row.
DELAY_NOTIFICATION_THRESHOLD_MINUTES = 5

# BUGFIX (expensive train/schedule queries): list_schedules,
# peak_hour_schedules, and delayed_schedules all ran `.all()` with no
# limit/offset at all - every Dispatch Board load (list_schedules is
# the single most-hit read on this router) pulled EVERY matching
# train_schedules row into memory and serialized it as one JSON
# response, growing linearly as more trains/stations get seeded. Same
# shape as the alert_service.py Phase 9 fix (DEFAULT_*_LIMIT/
# MAX_*_LIMIT + a hard-clamped offset/limit), applied here for the
# same reason: a default page size for normal callers, a hard upper
# bound so a caller can't force an unbounded fetch.
DEFAULT_SCHEDULE_LIST_LIMIT = 200
MAX_SCHEDULE_LIST_LIMIT = 1000

def _clamp(value: int | None, default: int, maximum: int) -> int:
    if value is None:
        value = default
    return min(max(value, 1), maximum)

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
    "platform_number", "station_sequence", "day_type", "is_peak_hour",
    "frequency_minutes", "status", "delay_minutes", "actual_arrival_time",
    "actual_departure_time",
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
    limit: int = DEFAULT_SCHEDULE_LIST_LIMIT,
    offset: int = 0,
) -> list[TrainSchedule]:
    """General schedule listing - the most-hit read on this router (every
    schedule-board load/refresh), but previously the only one of the
    three read paths here with zero caching (peak_hour_schedules/
    delayed_schedules already cached, this one still hit Postgres on
    every call). Cached the same way, keyed on the full filter set so
    different station/train/day/state combinations don't collide.

    BUGFIX (expensive train/schedule queries): also previously had no
    limit/offset - see DEFAULT_SCHEDULE_LIST_LIMIT/MAX_SCHEDULE_LIST_LIMIT
    above."""
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
    """Saturday/Sunday -> WEEKEND, else WEEKDAY. Schedules only carry a
    time-of-day (no date), so "today" is resolved this way rather than
    against a specific calendar date - matches how list_schedules'
    day_type filter is meant to be used.

    BUGFIX (naive datetime / timezone handling): this used to resolve
    "today" against the raw UTC weekday (`datetime.now(timezone.utc)`)
    on the theory that UTC was at least consistent with the rest of
    the app's timestamp policy. But train_schedule arrival/departure
    times are the metro network's own local wall-clock times (see
    app/utils/timezone.py), not UTC - so a train_schedules row that's
    WEEKDAY in local time could get compared against a UTC "today"
    that's already rolled over into Saturday (or vice versa) for
    several hours around each local midnight. Resolved against the
    app's configured business timezone instead, so this always agrees
    with what day it actually is for the network being scheduled."""
    return DayType.WEEKEND if business_now().weekday() >= 5 else DayType.WEEKDAY

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
        # BUGFIX (expensive train/schedule queries): `limit` was passed
        # straight through into `limit * 50` below with no clamp - a
        # caller passing a large `limit` turned into an equally large,
        # unbounded `LIMIT`/sort on train_schedules. Same clamp pattern
        # as the rest of this module.
        limit = _clamp(limit, 20, MAX_SCHEDULE_LIST_LIMIT)
        day_type = _current_day_type()
        # BUGFIX (naive datetime / timezone handling): compared against
        # a UTC time-of-day before, which - like _current_day_type()
        # above - is the wrong clock for schedule rows keyed on local
        # departure_time. See app/utils/timezone.py.
        now_t = business_now().time()

        # PERF FIX (query-analysis pass, see docs/query-performance-and-indexing.md):
        # this used to unconditionally `.join(Station, ...)` just to be
        # able to filter Station.city when a state/city scope was
        # requested - the same bug class _scope_to_state() above was
        # already written to avoid elsewhere in this module. That extra
        # Hash Join ran on every call regardless of whether `state` was
        # even given, for no benefit in the common (no state filter)
        # case. Joining Station only when `cities` is actually
        # non-empty removes that unnecessary join and lets the
        # ix_train_schedules_day_type_departure_time index do the
        # day_type/departure_time filtering via an index scan instead
        # of a full Seq Scan (a Sort can still appear afterward - the
        # separate joinedload() eager-load joins below don't guarantee
        # order - but the filter itself is no longer a table scan).
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
            """Since the timetable/history split (see
            app/models/train_schedule_history.py), train_schedules holds
            exactly one row per (train, station, day_type) slot, so a
            train legitimately appearing more than once here just means
            it has more than one upcoming stop in the selected scope -
            not a data bug. Still cap to one row per train for this
            widget: it's meant to read as a board of DIFFERENT next
            departures, and showing the same train's second/third stop
            further down the list adds noise without adding information
            (its "next stop" is already shown via the first occurrence).
            Kept defensive against pre-migration databases too, where a
            train/station/time slot could still have duplicate rows."""
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

        # Pull more raw rows than `limit` before deduping, since most of
        # them will collapse into the same handful of trains.
        candidates = (
            base.filter(TrainSchedule.departure_time >= now_t)
            .order_by(TrainSchedule.departure_time.asc())
            .limit(limit * 50)
            .all()
        )
        upcoming = _dedupe_by_train(candidates, limit)
        if not upcoming:
            # Nothing left for the rest of today under this filter -
            # fall back to the day's earliest matches so the widget
            # isn't empty right after the last train of the day departs.
            candidates = base.order_by(TrainSchedule.departure_time.asc()).limit(limit * 50).all()
            upcoming = _dedupe_by_train(candidates, limit)

        train_ids = {s.train_id for s in upcoming}
        if not train_ids:
            return []

        # Full same-day timetable for just these trains, to find each
        # picked row's next stop. Ordered by station_sequence (the
        # train's actual route order), NOT departure_time: two different
        # stations' scheduled times can coincide (and, on pre-migration
        # data with duplicate historical rows per slot, frequently did -
        # that's what previously made "next stop" resolve back to the
        # SAME station instead of the real next one). station_sequence
        # is unambiguous regardless. Rows from before this column
        # existed (station_sequence is nullable) sort last within their
        # train and are simply skipped as a "next stop" candidate below.
        #
        # BUGFIX (remaining N+1 query): this query used to have no
        # eager-load option at all, so `next_stop.station` below (read
        # for every train's "next stop" name) triggered one extra
        # lazy-loaded SELECT per row the very first time it was
        # accessed - up to `limit` (default 20, capped at
        # MAX_SCHEDULE_LIST_LIMIT) additional round trips on every call
        # to this dashboard-widget endpoint. `joinedload` here folds
        # that into the single query above via a JOIN, the same way
        # `base`'s query already does for `s.station` a few lines up.
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
                # Walk forward past any sibling with an equal or lower
                # station_sequence (shouldn't normally happen post-split,
                # but guards against stale/pre-migration rows) to the
                # first one that's a real later stop on the route.
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
    city could be filtered under).

    BUGFIX (expensive train/schedule queries): cache keys now carry the
    limit/offset the page was cached under (see
    DEFAULT_SCHEDULE_LIST_LIMIT/MAX_SCHEDULE_LIST_LIMIT above) - only
    the default (first-page) slice is invalidated here, same tradeoff
    as the per-state one: a caller paging past the first page gets a
    stale page for up to SCHEDULE_CACHE_TTL_SECONDS, which is an
    acceptable staleness window for a page nobody's default dashboard
    view actually lands on."""
    default_page = f"{DEFAULT_SCHEDULE_LIST_LIMIT}:0"
    cache.delete(f"schedule:list:None:None:None:None:{default_page}")
    cache.delete(f"schedule:list:None:{schedule.train_id}:None:None:{default_page}")
    cache.delete(f"schedule:list:{schedule.station_id}:None:None:None:{default_page}")
    cache.delete(f"schedule:peak:None:None:{default_page}")
    cache.delete(f"schedule:peak:{schedule.station_id}:None:{default_page}")
    cache.delete(f"schedule:delayed:None:None:{default_page}")
    cache.delete(f"schedule:delayed:{schedule.station_id}:None:{default_page}")

def _broadcast_schedule_update(db: Session, schedule: TrainSchedule) -> None:
    """Push a `schedule_update` event over the WebSocket so any open
    Dispatch Board / Train Scheduling tab reflects a create/update the
    moment it's committed, instead of waiting on the next
    SCHEDULE_CACHE_TTL_SECONDS-bounded poll.

    `SCHEDULE_UPDATE` (app/websocket/events.py) was defined and already
    typed on the frontend (useLiveSocket.ts's `LiveEvent` union) but no
    call site ever actually emitted it - create_schedule/update_schedule
    only wrote to Postgres, so a direct schedule create/edit (as opposed
    to the dedicated handle_delay/adjust_frequency workflows, which DO
    broadcast) never reached a connected client in real time. Mirrors
    handle_delay's DELAY_ALERT payload shape/lookups so the frontend can
    treat this the same way it already treats that event.
    """
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

    # BUGFIX (missing cache invalidation): a newly-created schedule used
    # to be invisible to list_schedules()/peak_hour_schedules()/
    # delayed_schedules() for up to SCHEDULE_CACHE_TTL_SECONDS whenever
    # an earlier request had already warmed the relevant cache key(s) -
    # the Dispatch Board kept showing the pre-create list. Same fix
    # shape as handle_delay/adjust_frequency below.
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

    # BUGFIX (missing cache invalidation): same gap as create_schedule -
    # a direct PUT edit (platform change, retimed departure, etc.) left
    # every already-cached list/peak/delayed view serving the pre-edit
    # row until the TTL expired, even though the delay/frequency-specific
    # workflows on this same model already invalidated correctly.
    _invalidate_schedule_caches(schedule)
    invalidate_route_cache()
    _broadcast_schedule_update(db, schedule)
    return schedule

def handle_delay(db: Session, schedule_id: int, payload: DelayUpdate) -> TrainSchedule:
    """Delay handling workflow: records delay minutes and flips status."""
    schedule = get_schedule(db, schedule_id)
    schedule.delay_minutes = payload.delay_minutes
    schedule.status = ScheduleStatus.DELAYED if payload.delay_minutes > 0 else ScheduleStatus.ON_TIME

    if payload.delay_minutes > 0:
        # BUGFIX (naive datetime / timezone handling): `datetime.today()`
        # anchored this on the naive server-local date - only the
        # resulting `.time()` is kept below, but the date component
        # should still come from the same business-timezone clock as
        # every other date/schedule calculation in this module rather
        # than an unrelated, naive one. See app/utils/timezone.py.
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
    limit: int = DEFAULT_SCHEDULE_LIST_LIMIT,
    offset: int = 0,
) -> list[TrainSchedule]:
    """Peak-hour optimization view: schedules currently flagged as peak.
    Cached (short TTL) - this is a frequently-refreshed dashboard view,
    same reasoning as the crowd dashboard snapshot.

    BUGFIX (expensive train/schedule queries): previously ran `.all()`
    with no limit/offset - see DEFAULT_SCHEDULE_LIST_LIMIT/
    MAX_SCHEDULE_LIST_LIMIT above."""
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
    delay_minutes hasn't been (re)recorded.

    BUGFIX (expensive train/schedule queries): previously ran `.all()`
    with no limit/offset - see DEFAULT_SCHEDULE_LIST_LIMIT/
    MAX_SCHEDULE_LIST_LIMIT above."""
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
