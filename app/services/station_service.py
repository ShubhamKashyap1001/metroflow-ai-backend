from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from app.models.line_station import LineStation
from app.models.station import Station
from app.schemas.station import StationCreate, StationUpdate
from app.utils.geo import cities_for_state

# Client-facing station list had no limit/offset at all - a caller (or
# a runaway frontend retry loop) could pull every row in the `stations`
# table in one response, unbounded as that table grows. The current
# dataset (63 real stations across 12 cities, or the ~6-station demo
# seed) is nowhere near this default, so normal callers - including
# the existing frontend, which never sends `limit`/`offset` today -
# see no change in behaviour. Same default-page + hard-cap pattern
# already used elsewhere (alert_service.list_alerts,
# notification_service.list_notifications, admin.py::get_logs).
DEFAULT_STATIONS_LIMIT = 500
MAX_STATIONS_LIMIT = 2000

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

def list_stations(
    db: Session,
    city: str | None = None,
    state: str | None = None,
    limit: int = DEFAULT_STATIONS_LIMIT,
    offset: int = 0,
) -> list[Station]:
    # SQLAlchemy 2.0 automatically wraps this in a subquery when
    # limit/offset is combined with a collection joinedload, so the
    # LIMIT still bounds distinct stations (not join-fanned-out rows)
    # - see StationResponse's metro_lines join above.
    limit = min(max(limit or DEFAULT_STATIONS_LIMIT, 1), MAX_STATIONS_LIMIT)
    offset = max(offset or 0, 0)

    query = db.query(Station).options(
        joinedload(Station.metro_lines).joinedload(LineStation.line)
    )
    cities = cities_for_state(state)
    if cities:
        query = query.filter(Station.city.in_(cities))
    elif city:
        query = query.filter(Station.city == city)
    stations = query.order_by(Station.station_name).offset(offset).limit(limit).all()
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
