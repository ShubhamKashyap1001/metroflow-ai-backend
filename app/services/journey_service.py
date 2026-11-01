
import math
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.enums.crowd_level import CrowdLevel
from app.models.crowd_log import CrowdLog
from app.models.journey import Journey
from app.models.station import Station
from app.enums.journey_status import JourneyStatus
from app.services.crowd_service import (
    apply_live_state_delta,
    broadcast_crowd_update,
    invalidate_station_cache,
)

BASE_FARE = 10.0
PER_KM_RATE = 2.0

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))

def _stage_crowd_delta(db: Session, station: Station, delta: int) -> tuple[int, CrowdLevel]:
    new_count, level = apply_live_state_delta(db, station, delta)
    db.add(CrowdLog(
        station_id=station.id,
        current_count=new_count,
        crowd_level=level,
    ))
    return new_count, level

def check_in(db: Session, user_id: str, source_station_id: int, destination_station_id: int) -> Journey:
    source = db.get(Station, source_station_id)
    destination = db.get(Station, destination_station_id)
    if not source or not destination:
        raise HTTPException(status_code=404, detail="Source or destination station not found")
    if source_station_id == destination_station_id:
        raise HTTPException(status_code=400, detail="Source and destination must differ")

    existing = active_journey_for_user(db, user_id)
    if existing:
        raise HTTPException(status_code=400, detail="You already have an active journey - check out first")

    journey = Journey(
        user_id=uuid.UUID(str(user_id)),
        source_station_id=source_station_id,
        destination_station_id=destination_station_id,
        checkin_time=datetime.now(timezone.utc),
        status=JourneyStatus.ACTIVE,
    )
    db.add(journey)

    new_count, level = _stage_crowd_delta(db, source, +1)

    try:
        db.commit()
    except IntegrityError:

        db.rollback()
        raise HTTPException(status_code=400, detail="You already have an active journey - check out first")
    db.refresh(journey)
    invalidate_station_cache(source)

    broadcast_crowd_update(source, new_count, level)
    return journey

def check_out(db: Session, user_id: str, journey_id: int) -> Journey:

    journey = (
        db.query(Journey)
        .filter(Journey.id == journey_id)
        .with_for_update()
        .one_or_none()
    )
    if not journey or str(journey.user_id) != str(user_id):
        raise HTTPException(status_code=404, detail="Active journey not found")
    if journey.status != JourneyStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Journey is not active")

    source = db.get(Station, journey.source_station_id)
    destination = db.get(Station, journey.destination_station_id)

    distance_km = haversine_km(
        source.latitude, source.longitude,
        destination.latitude, destination.longitude,
    )
    fare = round(BASE_FARE + distance_km * PER_KM_RATE, 2)

    journey.checkout_time = datetime.now(timezone.utc)
    journey.fare = fare
    journey.status = JourneyStatus.COMPLETED

    deltas = {source.id: -1, destination.id: +1}
    results = {}
    for station in sorted((source, destination), key=lambda s: s.id):
        results[station.id] = _stage_crowd_delta(db, station, deltas[station.id])
    source_count, source_level = results[source.id]
    destination_count, destination_level = results[destination.id]

    db.commit()
    db.refresh(journey)
    invalidate_station_cache(source)
    invalidate_station_cache(destination)
    broadcast_crowd_update(source, source_count, source_level)
    broadcast_crowd_update(destination, destination_count, destination_level)
    return journey

def active_journey_for_user(db: Session, user_id: str) -> Journey | None:
    return (
        db.query(Journey)
        .filter(Journey.user_id == user_id, Journey.status == JourneyStatus.ACTIVE)
        .order_by(Journey.checkin_time.desc())
        .first()
    )

