from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from app.models.line_station import LineStation
from app.models.station import Station
from app.schemas.station import StationCreate, StationUpdate
from app.utils.geo import cities_for_state

def _attach_line_info(stations: list[Station]) -> list[Station]:
    """Bolts line_name/line_color/station_order onto each Station
    instance from its metro_lines/line_stations join (see
    StationResponse) - not persisted, just read for this response.
    Picks the first associated line; every station in the current
    dataset belongs to exactly one."""
    for station in stations:
        link = station.metro_lines[0] if station.metro_lines else None
        station.line_name = link.line.line_name if link else None
        station.line_color = link.line.color if link else None
        station.station_order = link.station_order if link else None
    return stations

def list_stations(db: Session, city: str | None = None, state: str | None = None) -> list[Station]:
    query = db.query(Station).options(
        joinedload(Station.metro_lines).joinedload(LineStation.line)
    )
    cities = cities_for_state(state)
    if cities:
        query = query.filter(Station.city.in_(cities))
    elif city:
        query = query.filter(Station.city == city)
    stations = query.order_by(Station.station_name).all()
    return _attach_line_info(stations)

def get_station(db: Session, station_id: int) -> Station:
    station = (
        db.query(Station)
        .options(joinedload(Station.metro_lines).joinedload(LineStation.line))
        .filter(Station.id == station_id)
        .first()
    )
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    return _attach_line_info([station])[0]

def create_station(db: Session, payload: StationCreate) -> Station:
    existing = db.query(Station).filter(Station.station_code == payload.station_code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Station code already exists")

    station = Station(**payload.model_dump())
    db.add(station)
    db.commit()
    db.refresh(station)
    return station

def update_station(db: Session, station_id: int, payload: StationUpdate) -> Station:
    station = get_station(db, station_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(station, field, value)
    db.commit()
    db.refresh(station)
    return station

def delete_station(db: Session, station_id: int) -> None:
    station = get_station(db, station_id)
    db.delete(station)
    db.commit()
