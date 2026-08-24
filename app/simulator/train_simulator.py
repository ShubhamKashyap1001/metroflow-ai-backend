import asyncio
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.station import Station
from app.models.train import Train
from app.models.train_location import TrainLocation
from app.services.train_tracking import (
    build_routes,
    eta_seconds_for,
    segment_index_for,
    speed_factor_for,
)
from app.websocket.events import TRAIN_POSITION
from app.websocket.manager import manager

_train_state: dict[int, dict] = {}

def get_direction(train_id: int) -> int:
    return _train_state.get(train_id, {}).get("direction", 1)

def _locations_for(db: Session, train_ids: list[int], first_station: dict[int, int]) -> dict[int, TrainLocation]:
    if not train_ids:
        return {}

    existing = (
        db.query(TrainLocation)
        .filter(TrainLocation.train_id.in_(train_ids))
        .all()
    )
    by_train = {loc.train_id: loc for loc in existing}

    for tid in train_ids:
        if tid not in by_train and tid in first_station:
            loc = TrainLocation(
                train_id=tid,
                station_id=first_station[tid],
                next_station_id=first_station[tid],
                progress_ratio=0.0,
                status="at_station",
            )
            db.add(loc)
            by_train[tid] = loc

    db.flush()
    return by_train

def _track_tick_sync(db: Session, tick_seconds: int) -> list[dict]:
    trains = db.query(Train).filter(Train.is_active.is_(True)).all()
    train_ids = [t.id for t in trains]
    updates: list[dict] = []

    routes, segment_seconds, delay_by_station = build_routes(db, train_ids)
    first_station = {tid: r[0] for tid, r in routes.items() if len(r) >= 2}
    locations = _locations_for(db, train_ids, first_station)

    all_station_ids = {sid for route in routes.values() for sid in route}
    stations_by_id = {
        s.id: s
        for s in db.query(Station).filter(Station.id.in_(all_station_ids)).all()
    } if all_station_ids else {}

    for train in trains:
        route = routes.get(train.id, [])
        segs = segment_seconds.get(train.id, [])
        if len(route) < 2 or not segs:
            continue

        state = _train_state.setdefault(train.id, {"index": 0, "direction": 1})
        loc = locations.get(train.id)
        if loc is None:
            continue

        if loc.station_id in route:
            state["index"] = route.index(loc.station_id)

        index = state["index"]
        direction = state["direction"]

        current_delay = delay_by_station.get(train.id, {}).get(route[index], 0)
        speed_factor = speed_factor_for(current_delay)

        seg_index = segment_index_for(index, direction, len(segs))
        step = (tick_seconds / segs[seg_index]) * speed_factor

        progress = (loc.progress_ratio or 0.0) + step

        if progress >= 1.0:
            progress = 0.0
            index += direction
            if index >= len(route) - 1:
                index = len(route) - 1
                direction = -1
            elif index <= 0:
                index = 0
                direction = 1
            state["index"] = index
            state["direction"] = direction

        from_station_id = route[index]
        to_index = max(0, min(len(route) - 1, index + direction))
        to_station_id = route[to_index]

        loc.station_id = from_station_id
        loc.next_station_id = to_station_id
        loc.progress_ratio = progress
        loc.status = "at_station" if progress == 0.0 and from_station_id == to_station_id else "in_transit"

        from_station = stations_by_id.get(from_station_id)
        to_station = stations_by_id.get(to_station_id)

        current_delay = delay_by_station.get(train.id, {}).get(from_station_id, 0)
        speed_factor = speed_factor_for(current_delay)
        seg_index = segment_index_for(index, direction, len(segs))
        segment_duration = segs[seg_index]
        eta_seconds = eta_seconds_for(progress, segment_duration, speed_factor)

        updates.append({
            "train_id": train.id,
            "train_number": train.train_number,
            "from_station_id": from_station_id,
            "from_station_name": from_station.station_name if from_station else None,
            "to_station_id": to_station_id,
            "to_station_name": to_station.station_name if to_station else None,
            "progress_ratio": round(progress, 4),
            "delay_minutes": current_delay,
            "status": "Delayed" if current_delay > 0 else "Running",
            "eta_seconds": eta_seconds,
            "segment_duration_seconds": segment_duration,
            "direction": direction,
        })

    if updates:
        db.commit()

    return updates

async def track_tick(db: Session, interval_seconds: int) -> list[dict]:
    updates = await asyncio.to_thread(_track_tick_sync, db, interval_seconds)
    if updates:
        await manager.broadcast(
            TRAIN_POSITION,
            {"updates": updates, "timestamp": datetime.utcnow().isoformat()},
        )
    return updates

async def run_forever(session_factory, interval_seconds: int = 5) -> None:
    while True:
        db = session_factory()
        try:
            await track_tick(db, interval_seconds)
        except Exception as exc:                
            print(f"[train_simulator] tick failed, will retry next interval: {exc}")
        finally:
            db.close()
        await asyncio.sleep(interval_seconds)
