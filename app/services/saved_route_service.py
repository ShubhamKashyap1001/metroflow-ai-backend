
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session, aliased

from app.core import cache
from app.enums.schedule_status import ScheduleStatus
from app.models.saved_route import SavedRoute
from app.models.station import Station
from app.models.train_schedule import TrainSchedule
from app.schemas.saved_route import NextDeparture, SavedRouteLiveStatus
from app.services import crowd_service
from app.services.schedule_service import _current_day_type
from app.utils.timezone import business_now

LIVE_STATUS_CACHE_TTL_SECONDS = 15


def _station_name(db: Session, station_id: int) -> str:
    station = db.get(Station, station_id)
    return station.station_name if station else "Unknown station"


def _crowd_level(db: Session, station_id: int):
    """None if the station has no live crowd reading yet, or if the
    lookup itself fails - kept independently defensive (its own
    try/except) so a crowd-data hiccup only drops the crowd badge,
    not the next-train ETA this whole widget exists for."""
    try:
        snapshot = crowd_service.get_latest_crowd(db, station_id)
        return snapshot["crowd_level"] if snapshot else None
    except Exception:
        return None


def get_my_route(db: Session, user_id: str) -> SavedRoute | None:
    return (
        db.query(SavedRoute)
        .filter(SavedRoute.user_id == user_id)
        .first()
    )


def set_my_route(
    db: Session, user_id: str, origin_station_id: int, destination_station_id: int
) -> SavedRoute:
    if origin_station_id == destination_station_id:
        raise HTTPException(
            status_code=400,
            detail="Origin and destination stations must be different.",
        )
    for station_id in (origin_station_id, destination_station_id):
        if not db.get(Station, station_id):
            raise HTTPException(
                status_code=404, detail=f"Station {station_id} not found."
            )

    existing = get_my_route(db, user_id)
    if existing:
        existing.origin_station_id = origin_station_id
        existing.destination_station_id = destination_station_id
        route = existing
    else:
        route = SavedRoute(
            user_id=user_id,
            origin_station_id=origin_station_id,
            destination_station_id=destination_station_id,
        )
        db.add(route)

    db.commit()
    db.refresh(route)
    cache.delete(f"saved_route:live:{user_id}")
    return route


def delete_my_route(db: Session, user_id: str) -> None:
    existing = get_my_route(db, user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="No saved route to delete.")
    db.delete(existing)
    db.commit()
    cache.delete(f"saved_route:live:{user_id}")


def get_my_route_live(db: Session, user_id: str) -> SavedRouteLiveStatus | None:
    """Live next-departure/ETA for the user's saved route, or None if
    they haven't saved one yet (caller returns 404 for that case).
    """
    route = get_my_route(db, user_id)
    if not route:
        return None

    cache_key = f"saved_route:live:{user_id}"
    cached = cache.get_json(cache_key)
    if cached is not None:
        return SavedRouteLiveStatus.model_validate(cached)

    origin_name = _station_name(db, route.origin_station_id)
    dest_name = _station_name(db, route.destination_station_id)

    try:
        day_type = _current_day_type()
        now = business_now()
        now_t = now.time()

        origin_ts = aliased(TrainSchedule)
        dest_ts = aliased(TrainSchedule)

        directional = (
            db.query(origin_ts)
            .join(
                dest_ts,
                (dest_ts.train_id == origin_ts.train_id)
                & (dest_ts.station_id == route.destination_station_id)
                & (dest_ts.station_sequence.isnot(None))
                & (origin_ts.station_sequence.isnot(None))
                & (dest_ts.station_sequence > origin_ts.station_sequence),
            )
            .filter(
                origin_ts.station_id == route.origin_station_id,
                origin_ts.day_type == day_type,
                origin_ts.departure_time >= now_t,
                origin_ts.status != ScheduleStatus.CANCELLED,
            )
            .order_by(origin_ts.departure_time.asc())
            .first()
        )

        matched = True
        row = directional
        if row is None:
            matched = False
            row = (
                db.query(TrainSchedule)
                .filter(
                    TrainSchedule.station_id == route.origin_station_id,
                    TrainSchedule.day_type == day_type,
                    TrainSchedule.departure_time >= now_t,
                    TrainSchedule.status != ScheduleStatus.CANCELLED,
                )
                .order_by(TrainSchedule.departure_time.asc())
                .first()
            )

        next_departure = None
        message = None
        if row is not None:
            departure_dt = datetime.combine(now.date(), row.departure_time, tzinfo=now.tzinfo)
            eta_minutes = max(0, int((departure_dt - now).total_seconds() // 60))
            next_departure = NextDeparture(
                train_id=row.train_id,
                platform_number=row.platform_number,
                departure_time=row.departure_time,
                eta_minutes=eta_minutes,
                status=row.status,
                delay_minutes=row.delay_minutes,
                matches_destination=matched,
            )
            if not matched:
                message = "No direct match found for today - showing the next train from your station instead."
        else:
            message = "No more scheduled departures from your station today."

        result = SavedRouteLiveStatus(
            origin_station_id=route.origin_station_id,
            destination_station_id=route.destination_station_id,
            origin_station_name=origin_name,
            destination_station_name=dest_name,
            next_departure=next_departure,
            origin_crowd_level=_crowd_level(db, route.origin_station_id),
            destination_crowd_level=_crowd_level(db, route.destination_station_id),
            message=message,
        )
        cache.set_json(cache_key, result.model_dump(mode="json"), LIVE_STATUS_CACHE_TTL_SECONDS)
        return result
    except Exception:
        return SavedRouteLiveStatus(
            origin_station_id=route.origin_station_id,
            destination_station_id=route.destination_station_id,
            origin_station_name=origin_name,
            destination_station_name=dest_name,
            next_departure=None,
            message="Couldn't load live status right now - try again shortly.",
        )