
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
    """Apply a passenger check-in/out delta to `station`'s crowd count.

    A real check-in/check-out is a genuine, discrete event (not
    per-tick simulator noise), so - unlike the simulator's sampled
    history writes - it always:
      1. Atomically applies the delta to the live table
         (station_crowd_state) via apply_live_state_delta(), which
         takes a row lock so concurrent check-ins on the SAME station
         can't lose an increment to a race (see that function's
         docstring - this was verified as a real bug during Phase 2
         and fixed here).
      2. Stages ONE historical CrowdLog row (kept, not sampled/skipped)
         so inflow/outflow analytics still count every real passenger
         movement, not just the simulator's background CSV replay.

    Returns (new_count, level) so the caller can broadcast the same
    values it just persisted without an extra read.
    """
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
        # Belt-and-braces for the check-then-insert race above: two
        # simultaneous requests can both pass the active_journey_for_user()
        # check before either commits. The DB's partial unique index
        # (ux_journeys_one_active_per_user, one ACTIVE row per user_id)
        # lets exactly one of them win; the loser lands here instead of
        # creating a second ACTIVE journey.
        db.rollback()
        raise HTTPException(status_code=400, detail="You already have an active journey - check out first")
    db.refresh(journey)
    invalidate_station_cache(source)
    # Live flow: backend change -> WebSocket -> frontend, no refresh
    # needed. The passenger's own +1 is visible to every open
    # dashboard tab immediately, not just on the simulator's next tick.
    broadcast_crowd_update(source, new_count, level)
    return journey

def check_out(db: Session, user_id: str, journey_id: int) -> Journey:
    # SELECT ... FOR UPDATE locks this journey row for the duration of
    # the transaction (same fix already used for station crowd state -
    # see apply_live_state_delta's docstring). Without it, two
    # simultaneous checkout requests for the same journey can both
    # read status=ACTIVE before either commits, and both go on to
    # apply the fare/status update AND the crowd deltas below -
    # completing the journey twice and double-counting the
    # source/destination crowd change. The second request now blocks
    # here until the first commits, then re-reads status as COMPLETED
    # and is rejected by the check below instead of repeating the
    # update.
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

    # A checkout is a passenger LEAVING source and ARRIVING at
    # destination - both live counts must move, or destination
    # stations never reflect anyone who actually traveled there via
    # check-in/check-out (only the simulator's own CSV-replay
    # passengers would ever touch destination counts). Verified as a
    # real gap: this previously only staged the -1 at source and
    # never touched destination at all.
    #
    # Lock the two stations' rows in a fixed order (ascending id)
    # rather than source-then-destination: apply_live_state_delta
    # takes a row lock per station, and locking in travel order would
    # deadlock two concurrent checkouts going opposite directions
    # between the same pair of stations (A->B locks A then waits on
    # B, while B->A locks B then waits on A, at the same time).
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

