"""State-wise metadata for the frontend's navbar state selector.

Not a data module of its own - just aggregates Station/Train counts
per state so the UI knows which states have real, seeded data (and
which ones need more CSV rows before they're worth showing).
"""
import math

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.station import Station
from app.models.train import Train
from app.models.train_schedule import TrainSchedule
from app.utils.geo import MIN_STATIONS_FOR_SUFFICIENT_DATA, STATE_CITY_MAP, state_for_city

router = APIRouter(
    prefix="/meta",
    tags=["Meta"]
)

@router.get("/states")
def list_states(db: Session = Depends(get_db)):
    """One row per Indian state MetroFlow knows about, with how much
    real data is seeded for it - drives the navbar state picker and
    the "not enough data yet" banner on each dashboard."""
    station_counts = dict(
        db.query(Station.city, func.count(Station.id))
        .filter(Station.is_active.is_(True))
        .group_by(Station.city)
        .all()
    )

    train_counts = dict(
        db.query(Station.city, func.count(func.distinct(Train.id)))
        .join(TrainSchedule, TrainSchedule.station_id == Station.id)
        .join(Train, Train.id == TrainSchedule.train_id)
        .group_by(Station.city)
        .all()
    )

    results = []
    for state, cities in STATE_CITY_MAP.items():
        station_count = sum(station_counts.get(c, 0) for c in cities)
        train_count = sum(train_counts.get(c, 0) for c in cities)
        results.append({
            "state": state,
            "cities": cities,
            "station_count": station_count,
            "train_count": train_count,
            "has_sufficient_data": station_count >= MIN_STATIONS_FOR_SUFFICIENT_DATA,
        })

    return sorted(results, key=lambda r: r["state"])

@router.get("/cities")
def list_cities(db: Session = Depends(get_db)):
    """One row per city that actually has station data seeded - drives
    the navbar's city picker. A new city shows up here automatically
    the moment stations for it are added, no code change required."""
    station_counts = dict(
        db.query(Station.city, func.count(Station.id))
        .filter(Station.is_active.is_(True))
        .group_by(Station.city)
        .all()
    )

    train_counts = dict(
        db.query(Station.city, func.count(func.distinct(Train.id)))
        .join(TrainSchedule, TrainSchedule.station_id == Station.id)
        .join(Train, Train.id == TrainSchedule.train_id)
        .group_by(Station.city)
        .all()
    )

    city_to_state = {
        city: state
        for state, cities in STATE_CITY_MAP.items()
        for city in cities
    }

    results = []
    for city, station_count in station_counts.items():
        results.append({
            "city": city,
            "state": city_to_state.get(city),
            "station_count": station_count,
            "train_count": train_counts.get(city, 0),
            "has_sufficient_data": station_count >= MIN_STATIONS_FOR_SUFFICIENT_DATA,
        })

    return sorted(results, key=lambda r: r["city"])

def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km between two real lat/lng points."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))

@router.get("/nearest-city")
def nearest_city(
    lat: float = Query(..., ge=-90, le=90, description="Browser geolocation latitude"),
    lng: float = Query(..., ge=-180, le=180, description="Browser geolocation longitude"),
    db: Session = Depends(get_db),
):
    """Given the rider's real GPS coordinates (from the browser's
    Geolocation permission), finds the closest MetroFlow city using
    each seeded station's real latitude/longitude - no hardcoded city
    centroids, no fake data. Powers the "Use my location" control in
    the navbar city picker.
    """
    stations = (
        db.query(Station.city, Station.latitude, Station.longitude)
        .filter(Station.is_active.is_(True))
        .all()
    )
    if not stations:
        raise HTTPException(status_code=404, detail="No seeded stations to match against yet")

    best_city = None
    best_distance = None
    for city, station_lat, station_lng in stations:
        if station_lat is None or station_lng is None:
            continue
        distance = _haversine_km(lat, lng, station_lat, station_lng)
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_city = city

    if best_city is None:
        raise HTTPException(status_code=404, detail="No station coordinates available to match against")

    return {
        "city": best_city,
        "state": state_for_city(best_city),
        "distance_km": round(best_distance, 1),
    }
