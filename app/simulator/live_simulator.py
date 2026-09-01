"""Milestone 2 - "real-time operational monitoring" without any live
sensor/CCTV feed.

Previously this just overwrote each station's crowd count with a
noisy AI prediction every tick. It now simulates real passenger
traffic instead: a small pool of virtual passenger accounts actually
check in and check out through `journey_service` - the exact same
`POST /checkin` / `POST /checkout` code path a real ticket purchase
uses - so `current_count` moves up on check-in and down on check-out,
same as it would for a real user, and every tick also produces real
rows in the `journeys` table (the "Passenger Entry & Exit Records"
dataset the platform spec calls for).
"""
import asyncio
import random
import time
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.ai_engine.prediction.crowd_predictor import predict_crowd
from app.core.config import settings
from app.enums.journey_status import JourneyStatus
from app.enums.user_role import UserRole
from app.models.journey import Journey
from app.models.station import Station
from app.models.user_profile import UserProfile
from app.services import journey_service
from app.services.crowd_service import get_latest_crowd
from app.websocket.events import CROWD_UPDATE
from app.websocket.manager import manager

from app.simulator.constants import SIMULATED_EMAIL_DOMAIN

ASSUMED_SPEED_KMPH = 35
MIN_TRIP_MINUTES = 4

_stations_cache: list[dict] | None = None
_stations_cache_at: float = 0.0
_STATIONS_CACHE_TTL_SECONDS = 600

def _load_stations(db: Session) -> list[dict]:
    global _stations_cache, _stations_cache_at
    now = time.monotonic()
    if _stations_cache is not None and (now - _stations_cache_at) < _STATIONS_CACHE_TTL_SECONDS:
        return _stations_cache

    rows = db.query(Station).filter(Station.is_active.is_(True)).all()
    _stations_cache = [
        {
            "id": s.id,
            "station_name": s.station_name,
            "latitude": s.latitude,
            "longitude": s.longitude,
            "capacity": s.capacity,
        }
        for s in rows
    ]
    _stations_cache_at = now
    return _stations_cache

def _simulated_email(index: int) -> str:
    return f"passenger-{index:03d}@{SIMULATED_EMAIL_DOMAIN}"

def ensure_simulated_passengers(db: Session) -> list[UserProfile]:
    """Idempotently creates (or loads) the virtual passenger pool.
    Deterministic UUIDs (uuid5 of the email) so re-running this never
    creates duplicates across restarts."""
    pool_size = settings.SIMULATOR_PASSENGER_POOL_SIZE
    passengers = (
        db.query(UserProfile)
        .filter(UserProfile.email.like(f"%@{SIMULATED_EMAIL_DOMAIN}"))
        .all()
    )
    existing_emails = {p.email for p in passengers}

    created = False
    for i in range(1, pool_size + 1):
        email = _simulated_email(i)
        if email in existing_emails:
            continue
        user = UserProfile(
            id=uuid.uuid5(uuid.NAMESPACE_DNS, email),
            email=email,
            full_name=f"Simulated Passenger {i:03d}",
            role=UserRole.PASSENGER,
            is_active=True,
        )
        db.add(user)
        passengers.append(user)
        created = True

    if created:
        db.commit()

    return passengers

def _expected_travel_minutes(journey: Journey, stations_by_id: dict[int, dict]) -> float:
    source = stations_by_id.get(journey.source_station_id)
    destination = stations_by_id.get(journey.destination_station_id)
    if not source or not destination:
        return MIN_TRIP_MINUTES
    distance_km = journey_service.haversine_km(
        source["latitude"], source["longitude"],
        destination["latitude"], destination["longitude"],
    )
    return max(MIN_TRIP_MINUTES, (distance_km / ASSUMED_SPEED_KMPH) * 60)

def _simulate_tick_sync(db: Session) -> list[dict]:
    """All the actual DB/CPU work for one tick, as a plain sync
    function. Called via asyncio.to_thread() from simulate_tick() so
    this runs on a worker thread instead of blocking the single
    asyncio event loop that also serves every HTTP/WebSocket request -
    a synchronous SQLAlchemy Session doing several queries per station
    every tick was previously freezing the whole API (including login-
    adjacent requests like /auth/me) for the duration of each tick."""
    passengers = ensure_simulated_passengers(db)
    changed_station_ids: set[int] = set()
    stations = _load_stations(db)
    stations_by_id = {s["id"]: s for s in stations}

    active_journeys = (
        db.query(Journey)
        .filter(
            Journey.status == JourneyStatus.ACTIVE,
            Journey.user_id.in_([p.id for p in passengers]),
        )
        .all()
    )
    for journey in active_journeys:
        elapsed_minutes = (datetime.utcnow() - journey.checkin_time).total_seconds() / 60
        if elapsed_minutes >= _expected_travel_minutes(journey, stations_by_id):
            journey_service.check_out(db, user_id=str(journey.user_id), journey_id=journey.id)
            changed_station_ids.add(journey.source_station_id)
            changed_station_ids.add(journey.destination_station_id)

    if len(stations) >= 2:
        still_active_user_ids = {
            row[0]
            for row in db.query(Journey.user_id)
            .filter(
                Journey.status == JourneyStatus.ACTIVE,
                Journey.user_id.in_([p.id for p in passengers]),
            )
            .all()
        }
        idle_passengers = [p for p in passengers if p.id not in still_active_user_ids]

        weights = []
        for station in stations:
            prediction = predict_crowd(station["id"], datetime.utcnow(), light=True)
            weights.append(max(1.0, prediction["predicted_count"]))

        num_checkins = min(
            settings.SIMULATOR_MAX_CHECKINS_PER_TICK,
            len(idle_passengers),
        )
        chosen_passengers = random.sample(idle_passengers, num_checkins) if idle_passengers else []

        for passenger in chosen_passengers:
            source, destination = random.choices(stations, weights=weights, k=2)
            if source["id"] == destination["id"]:
                continue
            journey_service.check_in(
                db,
                user_id=str(passenger.id),
                source_station_id=source["id"],
                destination_station_id=destination["id"],
            )
            changed_station_ids.add(source["id"])

    if not changed_station_ids:
        return []

    updates = []
    for station_id in changed_station_ids:
        station = stations_by_id.get(station_id)
        latest = get_latest_crowd(db, station_id)
        if not station or not latest:
            continue
        updates.append({
            "station_id": station_id,
            "station_name": station["station_name"],
            "current_count": latest.current_count,
            "crowd_level": latest.crowd_level,
        })

    return updates

async def simulate_tick(db: Session) -> list[dict]:
    """Runs the sync tick body on a worker thread (so it never blocks
    the event loop), then broadcasts the result over the WebSocket
    from the loop itself - manager.broadcast() awaits a websocket
    send, which must happen on the real event loop, not a worker
    thread."""
    updates = await asyncio.to_thread(_simulate_tick_sync, db)
    if updates:
        await manager.broadcast(CROWD_UPDATE, {"updates": updates, "timestamp": datetime.utcnow().isoformat()})
    return updates

async def run_forever(session_factory, interval_seconds: int = 30) -> None:
    """Background loop: call once at startup with
    `asyncio.create_task(run_forever(SessionLocal))`.
    """
    while True:
        db = session_factory()
        try:
            await simulate_tick(db)
        except Exception as exc:                                                       
            print(f"[simulator] tick failed, will retry next interval: {exc}")
        finally:
            db.close()
        await asyncio.sleep(interval_seconds)
