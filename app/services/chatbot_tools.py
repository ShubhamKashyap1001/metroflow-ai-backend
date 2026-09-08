"""Real-data lookups the chatbot can call as tools, so it answers
data questions - station status, system-wide delays, active alerts,
busiest stations - from the actual database instead of letting the
model guess or invent numbers.

Every function here returns a plain JSON-able dict/list - it's fed
back to the Gemini API as a function response, never rendered
directly to the user, so field names/shape can stay whatever's most
useful for the model to read.
"""
from sqlalchemy.orm import Session, joinedload

from app.enums.day_type import DayType
from app.enums.schedule_status import ScheduleStatus
from app.models.station import Station
from app.models.train_schedule import TrainSchedule
from app.services import alert_service, crowd_service, schedule_service, station_service
from app.utils.timezone import business_now, business_today

# How many upcoming departures to surface for a "next train" answer -
# enough to also show the one after next without dumping the whole
# day's timetable at the model.
NEXT_TRAINS_LIMIT = 3

# How many candidate names to offer back when a station name doesn't
# match anything, so the assistant can ask "did you mean ...?"
# instead of silently guessing.
SUGGESTION_LIMIT = 5


def _current_day_type() -> DayType:
    """Saturday/Sunday -> WEEKEND, else WEEKDAY - matches the same
    rule schedule_service uses, kept local here so this module has no
    dependency on that module's private helper."""
    return DayType.WEEKEND if business_today().weekday() >= 5 else DayType.WEEKDAY


def _find_station(db: Session, name: str) -> Station | None:
    name = (name or "").strip()
    if not name:
        return None
    # Exact (case-insensitive) match first ...
    station = db.query(Station).filter(Station.station_name.ilike(name)).first()
    if station:
        return station
    # ... then fall back to a substring match ("kalighat" -> "Kalighat
    # Metro Station"), preferring active stations.
    return (
        db.query(Station)
        .filter(Station.station_name.ilike(f"%{name}%"))
        .order_by(Station.is_active.desc(), Station.station_name)
        .first()
    )


def _suggest_station_names(db: Session, name: str) -> list[str]:
    token = (name or "").strip().split(" ")[0]
    if not token:
        return []
    rows = (
        db.query(Station.station_name)
        .filter(Station.station_name.ilike(f"%{token}%"))
        .order_by(Station.station_name)
        .limit(SUGGESTION_LIMIT)
        .all()
    )
    return [r[0] for r in rows]


def _format_next_trains(schedules: list[TrainSchedule]) -> list[dict]:
    trains = []
    for s in schedules:
        entry = {
            "train_number": s.train.train_number if s.train else None,
            "platform_number": s.platform_number,
            "scheduled_arrival_time": s.arrival_time.strftime("%H:%M"),
            "status": s.status.value,
            "delay_minutes": s.delay_minutes,
        }
        if s.status == ScheduleStatus.DELAYED and s.delay_minutes:
            minutes_total = (s.arrival_time.hour * 60 + s.arrival_time.minute) + s.delay_minutes
            entry["estimated_arrival_time"] = f"{(minutes_total // 60) % 24:02d}:{minutes_total % 60:02d}"
        else:
            entry["estimated_arrival_time"] = entry["scheduled_arrival_time"]
        trains.append(entry)
    return trains


def get_station_status(db: Session, station_name: str) -> dict:
    """Everything about one station right now: crowd level/count and
    the next few scheduled arrivals (with live delay status). This is
    intentionally one combined lookup - a user asking "what's the
    status of X" usually means all of it, not just one field."""
    station = _find_station(db, station_name)
    if not station:
        return {
            "found": False,
            "query": station_name,
            "suggestions": _suggest_station_names(db, station_name),
        }

    full_station = station_service.get_station(db, station.id)

    crowd = crowd_service.get_latest_crowd(db, station.id)
    crowd_summary = (
        {
            "current_passenger_count": crowd["current_count"],
            "crowd_level": (
                crowd["crowd_level"].value
                if hasattr(crowd["crowd_level"], "value")
                else crowd["crowd_level"]
            ),
            "station_capacity": station.capacity,
            "last_updated": str(crowd["created_at"]) if crowd.get("created_at") else None,
        }
        if crowd
        else {"message": "No live crowd data recorded for this station yet."}
    )

    day_type = _current_day_type()
    now_t = business_now().time()

    upcoming = (
        db.query(TrainSchedule)
        .options(joinedload(TrainSchedule.train))
        .filter(TrainSchedule.station_id == station.id)
        .filter(TrainSchedule.day_type == day_type)
        .filter(TrainSchedule.status != ScheduleStatus.CANCELLED)
        .filter(TrainSchedule.arrival_time >= now_t)
        .order_by(TrainSchedule.arrival_time)
        .limit(NEXT_TRAINS_LIMIT)
        .all()
    )

    if upcoming:
        next_trains = _format_next_trains(upcoming)
        service_note = None
    else:
        # Nothing left today (past the last scheduled arrival) - show
        # tomorrow's first train(s) instead of returning nothing, but
        # say so explicitly so the model doesn't present it as "now".
        fallback = (
            db.query(TrainSchedule)
            .options(joinedload(TrainSchedule.train))
            .filter(TrainSchedule.station_id == station.id)
            .filter(TrainSchedule.day_type == day_type)
            .filter(TrainSchedule.status != ScheduleStatus.CANCELLED)
            .order_by(TrainSchedule.arrival_time)
            .limit(NEXT_TRAINS_LIMIT)
            .all()
        )
        next_trains = _format_next_trains(fallback)
        service_note = "No more scheduled arrivals for today - times below are tomorrow's first departures."

    return {
        "found": True,
        "station_name": full_station.station_name,
        "station_code": full_station.station_code,
        "city": full_station.city,
        "line_name": getattr(full_station, "line_name", None),
        "is_interchange": full_station.is_interchange,
        "crowd": crowd_summary,
        "next_trains": next_trains,
        "next_trains_note": service_note,
    }


# How many rows to hand back for system-wide list-style tools - kept
# small since these results are read by the model, not paginated by
# a human; the model can always ask again with a narrower `state`.
SYSTEM_WIDE_LIMIT = 15


def get_delayed_trains(db: Session, state: str | None = None) -> dict:
    """Every currently-delayed schedule entry system-wide (or scoped
    to one state/city if given) - for "which trains are delayed right
    now" / "any delays on the network" type questions that aren't
    about one specific station."""
    rows = schedule_service.delayed_schedules(db, state=state, limit=SYSTEM_WIDE_LIMIT)
    delays = []
    for s in rows:
        delays.append(
            {
                "train_number": s.train.train_number if s.train else None,
                "station_name": s.station.station_name if s.station else None,
                "city": s.station.city if s.station else None,
                "scheduled_arrival_time": s.arrival_time.strftime("%H:%M"),
                "status": s.status.value,
                "delay_minutes": s.delay_minutes,
            }
        )
    return {
        "scope": state or "all states",
        "delayed_count": len(delays),
        "delays": delays,
        "note": (
            f"Showing up to {SYSTEM_WIDE_LIMIT} delayed schedules; there may be more."
            if len(delays) == SYSTEM_WIDE_LIMIT
            else None
        ),
    }


def get_active_alerts(db: Session, state: str | None = None) -> dict:
    """Active (unresolved) service alerts - overcrowding, delay,
    emergency, maintenance, info - system-wide or scoped to one
    state/city. For "any service alerts / disruptions / maintenance
    right now" type questions."""
    rows = alert_service.list_alerts(
        db, active_only=True, state=state, limit=SYSTEM_WIDE_LIMIT
    )
    alerts = []
    for a in rows:
        station = db.get(Station, a.station_id)
        alerts.append(
            {
                "alert_type": a.alert_type.value,
                "message": a.message,
                "station_name": station.station_name if station else None,
                "city": station.city if station else None,
                "created_at": str(a.created_at),
                "available_until": str(a.available_until) if a.available_until else None,
            }
        )
    return {
        "scope": state or "all states",
        "active_alert_count": len(alerts),
        "alerts": alerts,
    }


def get_busiest_stations(db: Session, state: str | None = None) -> dict:
    """Current crowd snapshot across stations, busiest first - for
    "which stations are most crowded right now" type questions that
    aren't about one specific station."""
    snapshot = crowd_service.get_station_wise_snapshot(db, state)
    ranked = sorted(snapshot, key=lambda s: s.get("current_count", 0) or 0, reverse=True)
    top = ranked[:SYSTEM_WIDE_LIMIT]
    stations = []
    for s in top:
        station = db.get(Station, s.get("station_id"))
        crowd_level = s.get("crowd_level")
        stations.append(
            {
                "station_name": s.get("station_name"),
                "city": station.city if station else None,
                "current_passenger_count": s.get("current_count"),
                "crowd_level": crowd_level.value if hasattr(crowd_level, "value") else crowd_level,
            }
        )
    return {"scope": state or "all states", "busiest_stations": stations}
