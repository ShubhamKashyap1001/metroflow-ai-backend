from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core import cache
from app.models.station import Station
from app.models.train import Train
from app.models.train_location import TrainLocation
from app.models.train_schedule import TrainSchedule
from app.schemas.train import TrainCreate, TrainUpdate
from app.services.train_tracking import (
    build_routes,
    eta_seconds_for,
    segment_index_for,
    speed_factor_for,
)
from app.simulator.train_simulator import get_direction
from app.utils.geo import cities_for_state

def list_trains(db: Session, state: str | None = None) -> list[Train]:
    cities = cities_for_state(state)
    if not cities:
        return db.query(Train).order_by(Train.train_number).all()

    matching_train_ids = (
        db.query(TrainSchedule.train_id)
        .join(Station, Station.id == TrainSchedule.station_id)
        .filter(Station.city.in_(cities))
        .distinct()
        .subquery()
    )
    return (
        db.query(Train)
        .filter(Train.id.in_(db.query(matching_train_ids.c.train_id)))
        .order_by(Train.train_number)
        .all()
    )

def get_train(db: Session, train_id: int) -> Train:
    train = db.get(Train, train_id)
    if not train:
        raise HTTPException(status_code=404, detail="Train not found")
    return train

def create_train(db: Session, payload: TrainCreate) -> Train:
    existing = db.query(Train).filter(Train.train_number == payload.train_number).first()
    if existing:
        raise HTTPException(status_code=400, detail="Train number already exists")

    train = Train(**payload.model_dump())
    db.add(train)
    db.commit()
    db.refresh(train)
    return train

def update_train(db: Session, train_id: int, payload: TrainUpdate) -> Train:
    train = get_train(db, train_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(train, field, value)
    db.commit()
    db.refresh(train)
    return train

def list_live_positions(db: Session, state: str | None = None) -> list[dict]:
                                                                            
    cache_key = f"train:positions:{state or 'all'}"
    cached = cache.get_json(cache_key)
    if cached is not None:
        return cached

    positions = _list_live_positions_from_db(db, state)
    cache.set_json(cache_key, positions)
    return positions

def _list_live_positions_from_db(db: Session, state: str | None = None) -> list[dict]:
    trains = list_trains(db, state)
    train_ids = [t.id for t in trains]

    locations = (
        db.query(TrainLocation)
        .filter(TrainLocation.train_id.in_(train_ids))
        .all()
        if train_ids else []
    )
    trains_by_id = {t.id: t for t in trains}

    routes, segment_seconds, delay_by_station = build_routes(db, train_ids)

    all_station_ids = {loc.station_id for loc in locations} | {
        loc.next_station_id for loc in locations if loc.next_station_id
    }
    stations_by_id = {
        s.id: s
        for s in db.query(Station).filter(Station.id.in_(all_station_ids)).all()
    } if all_station_ids else {}

    results = []
    for loc in locations:
        train = trains_by_id.get(loc.train_id)
        if not train:
            continue

        from_station = stations_by_id.get(loc.station_id)
        to_station_id = loc.next_station_id or loc.station_id
        to_station = stations_by_id.get(to_station_id) if to_station_id else from_station

        current_delay = delay_by_station.get(train.id, {}).get(loc.station_id, 0)

        route = routes.get(train.id, [])
        segs = segment_seconds.get(train.id, [])
        eta_seconds = None
        segment_duration = None
                                                                   
        direction = get_direction(train.id)
        if route and segs and loc.station_id in route:
                                                                    
            index = route.index(loc.station_id)
            if to_station_id in route:
                to_index = route.index(to_station_id)
                if to_index != index:
                    direction = 1 if to_index > index else -1
            seg_index = segment_index_for(index, direction, len(segs))
            segment_duration = segs[seg_index]
            speed_factor = speed_factor_for(current_delay)
            eta_seconds = eta_seconds_for(loc.progress_ratio or 0.0, segment_duration, speed_factor)

        results.append({
            "train_id": train.id,
            "train_number": train.train_number,
            "from_station_id": loc.station_id,
            "from_station_name": from_station.station_name if from_station else None,
            "to_station_id": to_station_id,
            "to_station_name": to_station.station_name if to_station else None,
            "progress_ratio": loc.progress_ratio or 0.0,
            "delay_minutes": current_delay,
            "status": "Delayed" if current_delay > 0 else "Running",
            "eta_seconds": eta_seconds,
            "segment_duration_seconds": segment_duration,
            "direction": direction,
        })
    return results

def list_routes(db: Session, state: str | None = None) -> list[dict]:
    trains = list_trains(db, state)
    train_ids = [t.id for t in trains]
    routes, segment_seconds, _ = build_routes(db, train_ids)

    all_station_ids = {sid for route in routes.values() for sid in route}
    stations_by_id = {
        s.id: s
        for s in db.query(Station).filter(Station.id.in_(all_station_ids)).all()
    } if all_station_ids else {}

    results = []
    for train in trains:
        route = routes.get(train.id, [])
        if len(route) < 2:
            continue
        results.append({
            "train_id": train.id,
            "train_number": train.train_number,
            "station_ids": route,
            "station_names": [
                stations_by_id[sid].station_name if sid in stations_by_id else None
                for sid in route
            ],
            "segment_seconds": segment_seconds.get(train.id, []),
        })
    return results